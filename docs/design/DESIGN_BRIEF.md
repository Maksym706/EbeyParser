# EbeyParser — Product & Design Brief (v1, mouse-only UI)

**For:** the frontend team building the new Preact SPA (no build step), plus the backend devs who add the missing endpoints.
**Goal:** a polished consumer app at `http://localhost:8000` (and on the phone) where **everything is done with the mouse or a finger**:
first-time setup, searches, notifications, AI, and following each deal from "found" to "sold".
No CLI, no `config.yaml`, no `.env` editing, ever.

**Reading order:** §1 insights → §2 principles → §3 IA → §4 screens → §5 states → §6 design system → §7 copy → §8 backend gaps → §9 MVP scope → §10 sources.
UI copy is Russian and uses **«ты»**, as the README does. Explanations are in English.

> **Research limits:** reddit.com, App Store and most vendor pages could not be opened directly from the research environment
> (crawler or egress blocked). Community sentiment comes from search-engine summaries of those pages and from review
> aggregators and comparisons. Some of those comparisons are written by competitors (superflip.ai, flipifyapp.com,
> carsnipe.com), so treat their claims about rivals as biased. Design tokens were checked against public DESIGN.md
> breakdowns on GitHub. Every insight lists its sources; all URLs are in §10.

---

## 0. The product in one paragraph

A student in Berlin with little money runs EbeyParser on their own PC around the clock. It watches whole Kleinanzeigen
categories (and eBay.de through the official API) within a radius. For each new ad it estimates the market price from
its own price history and similar ads, and a **local** vision model checks the photos and text. When it finds something
it sends a clear instruction to Telegram or e-mail:
**«Покупай — прибыль ≈ +120 €»**, **«Торгуйся: предложи 300 €»**, **«Аукцион: ставь максимум 260 €»**.
There is also a «Для себя» mode: a wishlist, for example parts for a local-AI server, where the app shows savings rather than profit.

The app already has all of this data (see `ebeyparser/models.py`). What it lacks is a product UI that doesn't feel
like an admin panel.

---

## 1. Key user insights

Each insight has a source and a **→ design consequence**. S-numbers refer to §10.

### Speed & being first
1. **The first person to message the seller usually gets the deal.** Underpriced items get 10–30 messages in the first
   hour, and sellers stop answering once they have a few serious buyers. [S1, S2]
   → The feed's default sort is «Лучшие свежие», and every card shows **how old the ad is** («7 мин»).
   → One click copies a ready German message. Chat on Kleinanzeigen still happens by hand, so the fastest route is
   «Скопировать и открыть объявление».
2. **Built-in alerts are slow, and paid tools are slower than they claim.** Facebook's own alerts arrive 15 min to 4 h+
   late [S3]. Kleinanzeigen's saved-search push notifications are often hours late or never arrive [S4, S5]. One Swoopa
   subscriber paying $145 got alerts 10–30 minutes late, despite a promised "3 minutes or less" [S6, S7].
   Independent tests put paid tools at about 4–5 minutes median. [S8, S9]
   → Show the **real latency**: «найдено через 6 мин после публикации» when known, plus «следующая проверка через 12 мин».
   → Our cadence is limited by the ban-protection budget (README: about 30 min for 7 categories), so we don't promise
   seconds. We explain the trade-off with a load meter (§4.6, §4.7).
3. **Notifications must get through on the phone.** Kleinanzeigen users on Android and iOS report pushes that never
   arrive [S4, S5].
   → Telegram is the recommended channel, with a test message and a «Пришло?» confirmation during onboarding (§4.1.7).

### Noise control & trust in the numbers
4. **Alert fatigue kills these tools.** Loose filters send items outside the price range [S6]. Keyword-only scouts miss
   items or duplicate each other [S10]. The advice is to review false positives weekly, add exclusions, and tighten
   price or radius until almost every alert is worth opening [S9]. Tools advertise AI dedupe and junk filtering as a
   headline feature [S11].
   → Every alert shows **why** it passed. «Скрыть» asks for a one-tap reason («дорого», «не то», «развод», «уже продано»).
   → Global and per-search exclusion words are edited as chips.
   → Strategy presets set the threshold in one click: «Осторожно / Сбалансированно / Агрессивно» (§4.1.4).
5. **Flippers distrust profit numbers they can't check.** Reviews say some tools inflate profit and ignore eBay fees
   [S12, S13]. Competitors now sell "verified sold comps" math, e.g. "sold 14 times in 30 days at avg $187 → profit
   after fees $52" [S13, S14].
   → The deal view always shows the **market-price evidence**: the number of comparables, the range, the source
   («своя история цен» / «похожие объявления» / «продано на eBay»), a price-history chart, and a line-by-line
   **profit breakdown** (market − risk margin − fees − shipping − price).
   → Never show profit without saying where the market number came from.
6. **Scores need a plain label.** Deal-score apps pair 0–100 with words (Great/Good/Fair/Pass; 80+ = grab it,
   under 40 = walk away) and an AI-confidence percentage [S15, S16].
   → The label is the verb («Покупай», «Торгуйся», «Ставь до», «Подумай»). The score is secondary (a small number,
   plus a tooltip «87 из 100»).
7. **Sell-through matters as much as price.** Resellers treat a sell-through rate of 50%+ in 30 days as healthy and
   under 20% as slow [S17]. eBay's research tools show median, p25–p75 and sell-through [S17, S18].
   → The deal view shows **how liquid** the item is from our own history: «похожих объявлений за 60 дней: 23 · держатся в среднем 5 дней».
   Where eBay sold data exists, it adds «продано на eBay: 9».

### Negotiation & messaging
8. **The best first message is short, polite, and names a price.** Give a concrete amount, offer to pick up and pay
   cash, don't justify the lower price [S19, S20]. Kleinanzeigen etiquette: on «VB» ads a lower offer is fine if
   friendly, and repeated haggling is rude [S20, S21]. A bare "Noch da?" or "Is this available?" gets ignored
   [S2, S21].
   → The German composer (§7.4) defaults to *greeting + interest + concrete {offer} € + pickup day + cash*.
   A Russian translation appears underneath so the user knows what they're sending.
9. **Kleinanzeigen has «Angebot machen», «Direkt kaufen» and «Sicher bezahlen».** Buyers pay 0.50 € + 4.5% for
   buyer protection [S22, S23].
   → When the item needs shipping, the profit breakdown can add «Sicher bezahlen» fees, and a template asks for
   shipping via Sicher bezahlen.
10. **Auctions: set a maximum and let the proxy bid.** Snipers only place your max a few seconds before the end, and eBay
    charges the second-highest bid plus one increment [S24].
    → The auction card says «Ставь максимум 260 €», shows a live countdown, and gives a one-line tip:
    «ставь за 5–10 секунд до конца». It never says "bid now".

### Safety
11. **Common buyer scams on Kleinanzeigen:** Vorkasse, PayPal «Freunde & Familie» (no buyer protection), fake payment
    links and QR codes, moving the chat to WhatsApp or e-mail, prices that are too good to be true, new profiles
    without ratings, stolen stock photos [S25–S28].
    → Red flags (`Evaluation.red_flags` + `AIVerdict.red_flags` / `stock_photos` / `locked`) are shown **above** the
    profit numbers, in red, with a one-line reason.
    → A permanent «Безопасная сделка» tip block appears in the deal view (§4.3).
12. **Checks depend on the category.** iPhone: activation lock and IMEI. GPU: mining wear, stress test. Laptop: battery,
    BIOS password [S29, S30].
    → A «Проверь при встрече» checklist per product type (§7.5), copyable into the notes.

### Tracking the business
13. **Resellers want "true profit" after fees and shipping.** They track buy → listed → sold, time to sell, and
    dead stock. Reseller apps sell profit trackers, inventory aging and a "death pile helper" [S31, S32, S15].
    → The «Мои сделки» pipeline (Избранное → Написал → Купил → Продал) records the real buy and sell price.
    It compares **predicted and actual profit**, which also shows the user how much to trust the app.
14. **Price-history UX that people know: Keepa and CamelCamelCamel.** Line chart of price over time, range tabs
    (week / month / 3 months / year / all), hover tooltip, toggleable series, a target-price alert [S33, S34, S35].
    → The deal view gets a small Keepa-style chart: asking prices of the same product over time, a median line, a
    p25–p75 band, «выгодно до» as a dashed line, and this ad as a highlighted dot (§4.3.6).

### Setup pain (our own users)
15. **Self-hosted Kleinanzeigen bots all require Docker, env vars or CLI** (e.g. `kleinanzeigen-alert`: "configure token
    in compose file, run container") [S36, S37]. Our README currently asks for PowerShell, `python -m ebeyparser setup`,
    `ai-check --fix` and YAML edits. The target user is non-technical and **angry** about this.
    → Onboarding in the browser (§4.1). Every integration has a **«Проверить»** button that returns a human result.
    Telegram's chat id is **auto-detected** (§4.1.7).
16. **Onboarding best practice:** 3–6 steps, a visible progress indicator, «Назад» always available, optional steps
    skippable, and a 3–5 item checklist tied to the user's goal (not the feature list) that stays visible until
    finished [S38, S39, S40].
    → 9 short screens, 3 of them skippable. After setup, a «Осталось настроить» checklist card stays on the feed until
    Telegram and AI are done.

---

## 2. UX principles (non-negotiable)

1. **No CLI, ever.** Nothing in the UI tells the user to run a command, edit a file, or "restart the app". If the backend
   needs a restart after a change, the UI does it with a button («Перезапустить проверки») and a progress state.
   Text such as `python -m ebeyparser …` and `config.yaml` appears **only** inside the «Для продвинутых» disclosure in
   Settings → Данные.
2. **Every integration has a test button with a human result.** «Проверить» → spinner (≤ 300 ms, then a label such as
   «Жду ответа от LM Studio…») → green / amber / red result card. Each card says in one sentence what happened and what
   to do. Raw errors are hidden under «Подробности».
3. **Explain inline, not in docs.** Each non-obvious field has a 1-line helper under it and a «?» popover with an example.
   Numbers get a live example («Пример: iPhone 13 на рынке ~420 € → уведомлю, если цена ≤ 305 €»).
4. **Sane defaults, preselected.** Berlin + 30 km, the 7 recommended categories, «Сбалансированно», check every 30 min,
   Telegram instant, AI on if detected. The user can finish onboarding by pressing «Дальше» on every screen.
5. **Decision first, evidence second.** Every deal surface leads with the action verb and the money number. Evidence
   (chart, comparables, AI findings) follows in that order.
6. **Colour means money, always the same way.** Green = profit / buy. Amber = haggle / offer. Violet = auction / bid.
   Red = danger / scam. Blue = info / AI. Neutral = maybe / skip. Don't use these hues for decoration (§6.1).
7. **Destructive actions confirm; everything else undoes.** Hide, star and status moves are instant, with a 6 s
   «Отменить» toast. Deleting a search, resetting price history, and changing phone-access mode use a confirm dialog.
   Wiping data requires typing «удалить».
8. **Live, not reload.** New deals arrive through server events. They show a toast («Новая находка: +95 € · iPhone 12»),
   a pill at the top of the feed («↑ 2 новых»), and the tab title changes to «(2) EbeyParser». The feed never jumps
   under the cursor.
9. **Every screen has four designed states:** loading (skeleton), empty (explain + next action), error (what + fix
   button), and partial/offline (stale banner). See §5.
10. **Phone is first-class.** Bottom tabs, thumb-reachable sticky action bar, 44 px touch targets, 16 px inputs
    (prevents iOS zoom), no hover-only affordances.
11. **Honest about the machine.** Learning mode, cooldowns after blocks, AI being down, and queued work are all visible
    in plain Russian with an ETA. The user never has to guess whether the app is alive.
12. **Private by default.** Everything stays local. No CDN, fonts or analytics from the internet; all assets are vendored
    (§6.10). Secrets are masked (`12••••••:AA••••x9`) and never sent back to the client in full.

---

## 3. Information architecture

### 3.1 Navigation

| Destination | RU label | Lucide icon | Route | Badge |
|---|---|---|---|---|
| Deals feed (home) | **Лента** | `zap` | `#/` | count of unseen good deals |
| My deals pipeline | **Мои сделки** | `wallet` | `#/deals` | count «Написал» older than 24 h (needs follow-up) |
| Searches | **Поиски** | `radar` | `#/searches` | — |
| Health | **Состояние** | `activity` | `#/health` | red dot if something is broken |
| Settings | **Настройки** | `settings` | `#/settings/:section` | amber dot if setup incomplete |

- **Desktop (≥ 1024 px):** inverted-L layout [S41]. Left sidebar is 240 px (collapsible to a 64 px icon rail). It holds
  the logo, the 5 destinations, a mini status block at the bottom («● Работает · след. проверка 12:40» + «Проверить
  сейчас») and the theme toggle. The topbar is 56 px, inside the content column, with the page title, search field,
  primary page action and a notification bell. The sidebar surface is one step dimmer than content, so content wins [S41].
- **Tablet (600–1023 px):** 64 px icon rail on the left with labels in tooltips, and the same topbar [S42].
- **Phone (< 600 px):** top app bar (48 px: title + 1–2 icon actions) and a **bottom tab bar** with the same 5
  destinations. The bar is 64 px + safe-area inset; icon 24 + label 11 px; the active tab uses a filled pill behind the
  icon [S42]. Material limits bottom bars to 3–5 destinations, so 5 is the maximum.
- **Onboarding** runs full-screen without app chrome (§4.1).
- **Deal detail** is a URL (`#/deal/:id`), so a Telegram link can open it directly. Desktop shows it as a drawer over the
  current list; phone shows it full-screen.

### 3.2 Screen list

| # | Screen | Route | Notes |
|---|---|---|---|
| 1 | Onboarding (9 steps) | `#/welcome/:step` | Forced when no searches exist; can be re-run from Settings |
| 2 | Лента (deals feed) | `#/` | Filters in query: `#/?f=haggle&sort=fresh` |
| 3 | Сделка (deal detail) | `#/deal/:adId` | Drawer ≥ 1024 px, full screen on phone |
| 4 | Мои сделки (pipeline) | `#/deals/:column?` | Kanban ≥ 1024 px, segmented list on phone |
| 5 | Поиски | `#/searches` | Edit in side panel: `#/searches/:name` |
| 5a | Новый поиск | `#/searches/new/:kind` | kind = category / keywords / url / ebay / wish |
| 6 | Состояние | `#/health` | Logs: `#/health/logs` |
| 7 | Настройки | `#/settings/:section` | sections listed in §4.7 |
| 8 | Проверить объявление | modal from topbar | Paste any Kleinanzeigen/eBay URL → evaluation (replaces `ebeyparser check <url>`) |
| 9 | 404 / offline | — | Friendly, with «На главную» |

### 3.3 Global elements
- **Topbar search** (desktop) filters the current list. `/` focuses it.
- **«Проверить объявление»** (topbar icon `scan-search`): paste a link and get the same decision block as §4.3.
  This replaces the `check` CLI command.
- **Toasts**: bottom-center on phone, bottom-right on desktop (§6.8.10).
- **Keyboard (desktop, should):** `j/k` next/previous deal, `Enter` open, `Esc` close, `s` star, `h` hide, `c` copy
  message, `o` open original, `?` shortcut sheet. Raycast-style keycaps [S43].

---

## 4. Screen-by-screen specs

### 4.1 First-run onboarding

**Layout.**
- **Desktop:** centered 2-pane card, max 960 × 640. The left pane (280 px, surface-2) is a vertical stepper: step names,
  a check icon on completed steps, «необязательно» under optional ones. Completed steps are clickable (non-linear back
  navigation [S38]). The right pane holds the content and a footer bar: «Назад» (ghost) left; «Пропустить» (ghost,
  optional steps only) and «Дальше» (primary) right.
- **Phone:** full screen. Top: «Шаг 3 из 8» + thin progress bar (4 px). Content scrolls. The sticky bottom bar has
  «Назад» (icon button) and a full-width «Дальше».
- **Draft saving:** answers are kept in `localStorage` (`ebp.onboarding.v1`) and on the server
  (`PUT /api/setup/draft`), so a reload or phone switch doesn't lose progress. Nothing is written to config until
  «Запустить». Telegram, AI and eBay checks run live and store their secrets immediately (to `.env`, server-side) once
  validated.
- **Step header anatomy:** 40 px icon tile (tinted) → title (h1 24/30) → 1-line subtitle (body, text-2).
- **Motion:** content slides 24 px + fades between steps (240 ms). Reduced motion: fade only.

#### 4.1.1 Welcome
- **Illustration:** the logo plus three benefit rows, each with an icon:
  - `radar` «Смотрю все новые объявления в твоём районе 24/7»
  - `scan-eye` «Проверяю фото и описание локальной нейросетью — бесплатно и приватно»
  - `send` «Присылаю в Telegram только то, что правда выгодно, и сколько предложить»
- **Title:** «Привет! Найдём, что можно выгодно купить»
- **Subtitle:** «Настройка займёт 3–5 минут. Всё можно поменять потом.»
- **CTA:** «Начать» (primary, large). Secondary link: «Сначала посмотреть на примере» loads demo data, replacing the
  `demo` CLI command. The feed then shows a «Демо-данные» ribbon with «Убрать демо» and «Настроить по-настоящему».

#### 4.1.2 Где искать (Where)
- **City field:** autocomplete (debounced 250 ms, `GET /api/locations?q=`), accepts a city or postal code, placeholder
  «Berlin или 10115», default «Berlin». A suggestion shows the name + a small grey line
  («Berlin · Kleinanzeigen-ID 3331»). An invalid entry gets an inline error: «Не нашёл такой город — попробуй индекс».
- **Radius slider** with discrete stops matching Kleinanzeigen: `0 (только город) · 5 · 10 · 20 · 30 · 50 · 100 · 150 · 200 км`,
  default 30. The value label floats above the thumb («30 км»).
- **Visual:** a simple SVG disc (no map tiles; offline) that grows with the radius. Caption: «≈ до 40 минут на S-Bahn»
  (static heuristic by radius), or «примерно весь Берлин».
- **Helper:** «Чем больше радиус — тем больше находок, но дальше ехать за покупкой.»
- **Validation:** city required. «Дальше» is disabled until a suggestion is chosen or the backend confirms the text.

#### 4.1.3 Что искать (What)
- **Grid of category cards:** 2 columns on phone, 3–4 on desktop. Card = 40 px icon, DE name (15/600), RU description
  (13, text-2), and a checkbox tick in the top-right corner. Icons: see §6.9.
  - The 7 recommended cards are **preselected**, with a small green «рекомендуем» pill.
  - After «Сверить с сайтом» (link button, `GET /api/categories?location=&radius=&live=1`), each card shows the live
    count «1 240 объявлений». While it loads, the cards show skeleton counts.
  - A selected card gets a green-tinted background, a 1.5 px green border and a filled check.
- **Top toolbar:** «Только рекомендуемые» · «Снять все» · counter «Выбрано: 7».
- **Load meter:** at the bottom, «Нагрузка на Kleinanzeigen», a bar filling toward the hourly request cap. It shows
  the interval the app will use: «Проверка каждые 30 мин — безопасно». If more than 8 categories are selected, the bar
  turns amber: «Много категорий: проверять буду раз в 45 мин, чтобы не заблокировали». The server computes this
  (`GET /api/setup/estimate`); it's the same 40% logic as the README.
- **Validation:** at least 1 category or at least 1 wishlist item (next step). Otherwise the helper shows
  «Выбери хотя бы одну категорию — или добавь, что ищешь для себя, на следующем шаге».

#### 4.1.4 Деньги (Money)
- **Purpose:** 3 segmented cards.
  - «Перепродажа» — «купить дешевле рынка и продать»
  - «Для себя» — «купить дешевле рынка себе»
  - «И то и другое» (default)
- **«Сколько готов потратить на одну вещь»:** slider 50–1 500 € (step 10) plus a linked number input. Default 400 €.
  Maps to per-search `max_price` and `pricing.max_capital`.
- **Strategy presets:** 3 large radio cards, each with an icon, name, one line, and «≈ N уведомлений в день»
  (a server estimate once there is history; before that, static text):

  | Preset | Icon | Line | min_profit | min_roi | safety_margin_percent | min_comparables | notifications.min_score |
  |---|---|---|---|---|---|---|---|
  | **Осторожно** | `shield` | «Только очень выгодное, меньше уведомлений» | 60 € | 0.35 | 15 | 8 | 80 |
  | **Сбалансированно** (default) | `scale` | «Золотая середина» | 40 € | 0.25 | 10 | 6 | 70 |
  | **Агрессивно** | `rocket` | «Больше находок, больше проверять самому» | 25 € | 0.15 | 7 | 4 | 60 |

- **«Настроить вручную»** disclosure: min profit (€), min ROI (%), risk margin (%). Changing any of them switches the
  preset chip to «Свой».
- **Live example box** (blue-tinted), computed client-side from the preset:
  «Пример: iPhone 13 128 GB стоит на рынке ~420 €. Уведомлю, если цена до **305 €** (прибыль от 40 €).»
- **Fees:** a small line «Комиссии при продаже: 0 % (частные продавцы не платят)» and a «Изменить» link to
  Settings → Деньги. Kept out of onboarding.

#### 4.1.5 Для себя (Wishlist), optional
- **Title:** «Ищешь что-то для себя?». Subtitle: «Например, железо для AI-сервера. Буду следить и скажу, когда цена ниже рынка.»
- **Rows:** `[что ищешь]` `[до … €]` `[× remove]`, plus «+ Добавить».
  - Each row also has a toggle «показывать и чуть дороже (+20 %) — для торга», on by default. It maps to
    `target_price` + `max_price = target * 1.2`.
- **Suggestion chips** (tap to add a row with a typical price placeholder): «RTX 3090 · 550 €», «RTX 3060 12GB · 200 €»,
  «DDR4 ECC 64GB · 90 €», «Ryzen 9 5950X · 250 €», «Блок питания 1000W · 80 €», «Серверный корпус · 60 €».
- **Footer:** «Пропустить» is prominent, because the step is optional.

#### 4.1.6 Нейросеть (Local AI)
- **On enter:** auto-detect (`GET /api/ai/detect`, already exists). The UI shows «Ищу LM Studio и Ollama на этом
  компьютере…» with a skeleton card for ≤ 3 s.
- **Result variants:**
  - **A. Found + vision model** (green card, `circle-check`): «Нашёл LM Studio · модель видит фото».
    A model dropdown lists vision models first; the recommended one is marked «рекомендуем».
    Button **«Проверить на примере»** calls `POST /api/ai/test` (new), which runs the local model on a bundled sample
    ad (photo + German text). The result card shows:
    «Модель увидела: *iPhone 13, 128 GB* · состояние *хорошее* · фото совпадают с описанием ✓ · ответ за **14 с**».
    - Slower than 90 s → amber note: «Медленно. На слабом ПК выбери модель поменьше (3B) — вот какую».
    - Wrong or empty answer → amber: «Модель ответила странно. Поставь Context Length 8192 в LM Studio (картинка-подсказка)».
  - **B. Server found, no vision model** (amber): «LM Studio работает, но нет модели, которая понимает фото».
    Steps: 1) open Discover; 2) find `Qwen2.5-VL-7B-Instruct` (copy chip); 3) load it with Context Length 8192.
    «Проверить снова» polls every 5 s while the step is open.
  - **C. Nothing found** (neutral): three numbered mini-steps with small illustrative screenshots
    (static SVG mock-ups, no external images):
    1. «Скачай LM Studio» (external link lmstudio.ai)
    2. «Во вкладке Discover скачай Qwen2.5-VL-7B»
    3. «Developer → Start Server и включи автозапуск»

    Plus a «Какая у тебя видеокарта?» chooser (chips: «10–12 ГБ», «6–8 ГБ», «Слабый ПК», «16–24 ГБ») that recommends a
    model, from the README table. Polling runs every 5 s: «Как только запустишь сервер — я сам увижу».
- **Skip:** «Продолжить без нейросети» asks for confirmation in a small popover: «Без нейросети фото никто не
  проверит — уведомления придут с пометкой ⚠. Можно включить потом.»

#### 4.1.7 Telegram (guided)
One screen with a **vertical checklist of 4 sub-steps**. Each completes with a green check; only the current one is
expanded (accordion). The subtitle is «Самый быстрый способ узнать о находке — сообщение с фото прямо в телефон».

1. **Создай бота.** Button «Открыть @BotFather» (`https://t.me/BotFather`, new tab/app) + copy chip `/newbot`.
   Helper: «Придумай любое имя, а username должен заканчиваться на *bot*, например *maks_deals_bot*. BotFather пришлёт
   длинный ключ — скопируй его.»
2. **Вставь ключ.** Password-style input with a «Вставить» button (clipboard API; hidden if unavailable).
   - Instant format check `^\d{6,12}:[A-Za-z0-9_-]{30,}$`; if it fails: «Похоже, скопировалось не всё — ключ выглядит
     так: 123456789:AA…».
   - Then `POST /api/telegram/validate` (server calls `getMe`) → «✓ Бот **@maks_deals_bot** найден».
     Error: «Telegram не узнал этот ключ — скопируй его у BotFather ещё раз».
3. **Напиши боту.** Primary button «Открыть бота и нажать Start» → deep link `https://t.me/<username>?start=<code>`.
   The `<code>` is a 16-char random token from the server [S44].
   - The UI shows a live waiting state: «Жду твоё сообщение… (до 2 минут)» with an animated dot. It polls
     `GET /api/telegram/detect?code=` every 2 s; the server calls `getUpdates` and matches `/start <code>` [S45].
   - Success: «✓ Нашёл тебя: **Maksim** (личный чат)». The chat id is stored server-side and never shown in full.
   - Fallback (after 2 min or «Не получается»): manual chat-id field + «Узнать chat_id можно у @userinfobot». If a
     webhook blocks `getUpdates`, the server returns a specific error and the UI offers «Сбросить webhook бота».
4. **Проверим.** «Отправить тестовое сообщение» → `POST /api/notify/test?channel=telegram` sends a sample deal with a
   photo. Then the question «Пришло сообщение?» with «Да, пришло 🎉» / «Нет».
   «Нет» expands tips: Telegram notifications for the bot, not muted, and on Android turn off battery optimisation for
   Telegram.
- **Alternatives:** «Лучше на почту» opens an inline Gmail app-password mini-guide (link to
  myaccount.google.com/apppasswords, fields e-mail + app password + «Проверить»). «Настрою позже» (skip) is allowed but
  leaves a checklist item on the feed.

#### 4.1.8 eBay (optional)
- **Title:** «Добавить eBay?». Subtitle: «Аукционы и «Sofort-Kaufen» с eBay.de — через официальный API, бесплатно. Можно пропустить.»
- **Collapsed guide with 3 steps:**
  1. «Зарегистрируйся на developer.ebay.com» (link)
  2. «Открой *My Keys* → создай *Production keyset*» (link to developer.ebay.com/my/keys)
  3. «На вопрос про *Marketplace Account Deletion* выбери исключение **“I do not persist eBay data”**»,
     with an info popover explaining why that is true for EbeyParser.
- **Fields:** «App ID (Client ID)», «Cert ID (Client Secret)» (masked).
- **Check:** «Проверить» → `POST /api/ebay/validate` (server gets an OAuth token and reads rate limits) →
  «✓ Ключи работают · лимит 5 000 запросов в день».
  Errors, humanised:
  - «eBay не принял ключи — проверь, что это *Production*, а не *Sandbox*»
  - «Ключи ещё не активированы — ответь на вопрос про Account Deletion»

#### 4.1.9 Готово (Done)
- **Summary list** of what's configured, each row with a status icon and an «Изменить» link back to its step:
  - «Berlin + 30 км»
  - «7 категорий · 1 желание»
  - «Сбалансированно · до 400 € за вещь»
  - «Нейросеть: qwen2.5-vl-7b ✓»
  - «Telegram ✓»
  - «eBay — пропущено»
- **«Что будет дальше»:** a horizontal 3-step timeline (vertical on phone):
  1. `hourglass` **«Сейчас: изучаю рынок.»** «Первая проверка только собирает цены — уведомлений не будет. Это 10–30 минут.»
  2. `radar` **«Потом: каждые 30 мин»** «смотрю только новые объявления».
  3. `send` **«Нашёл выгодное — пишу в Telegram»** «с фото и суммой, которую предложить».
- **Primary CTA:** **«Запустить и открыть ленту»**. This saves the config atomically (`POST /api/setup`), starts the
  monitor (`POST /api/run`) and routes to `#/`.
- On save failure: an inline error card with «Попробовать ещё раз». The draft is kept.

### 4.2 Лента: Deals feed (home)

```
Desktop ≥1280                                                       Phone <600
┌────────┬───────────────────────────────────────────────┐         ┌──────────────────────┐
│ LOGO   │ Лента           [🔍 Найти: 3090…]  [Проверить] │         │ Лента          🔍  ⟳ │
│ ⚡Лента │ ┌───────────────────────────────────────────┐ │         │┌────────────────────┐│
│ 👛Мои  │ │ HERO: Сегодня 12 выгодных · лучшее ▸      │ │         ││ Сегодня 12 выгодных ││
│ 📡Поиски│ │ [img] iPhone 13 128GB  290 € → +110 €     │ │         ││ лучшее: iPhone 13…  ││
│ 〰Сост. │ │ ● Работает · след. проверка через 12 мин  │ │         │└────────────────────┘│
│ ⚙Настр.│ └───────────────────────────────────────────┘ │         │ (chips scroll →)     │
│        │ [Все выгодные][Купить][Торг][Аукционы][Для   │         │ ┌──┐ Title 2 lines    │
│        │  себя][≤10 км][Без ⚠] [⚙ Ещё]   Сорт: Лучшие ▾│         │ │▢ │ 290 € VB  ~420   │
│        │ ┌─────┐┌─────┐┌─────┐┌─────┐                  │         │ └──┘ [+110 € прибыль]│
│ ● Раб. │ │card ││card ││card ││card │  (grid)          │         │ 4 км · 7 мин · ✓ИИ   │
│ 12:40  │ └─────┘└─────┘└─────┘└─────┘                  │         ├──────────────────────┤
└────────┴───────────────────────────────────────────────┘         │ ⚡  👛  📡  〰  ⚙    │
                                                                   └──────────────────────┘
```

#### 4.2.1 Hero summary (top, one card, 16 px radius)
- **Left column:**
  - Overline «СЕГОДНЯ».
  - Headline: «Найдено **12** выгодных» (32/700, tnum), or in personal-only mode «**3** находки для себя».
  - Sub-line: «Потенциал **+740 €** · просмотрено 1 312 объявлений».
- **Right column: «Лучшее сегодня» mini-card.** 64 px thumbnail, title (1 line), «290 € → **+110 €**» and an «Открыть» chevron.
- **Status strip** at the bottom of the hero:
  - State dot (green pulse = running, amber = cooldown or AI down, grey = paused).
  - Text: «Работает · следующая проверка через 12 мин» or «Идёт проверка… 3 из 8 поисков». The countdown updates every second.
  - Buttons: «Проверить сейчас» (ghost) and «Пауза» (icon).
- **Learning state:** when a search is still in its baseline run, the hero becomes a progress card.
  «Изучаю рынок: 3 из 7 категорий · собрано 412 цен · первые уведомления после следующей проверки (~12:40)»,
  with a determinate bar.
- **Setup checklist card** (below the hero, dismissible only when complete): «Осталось настроить: □ Telegram (2 мин)
  □ Нейросеть». Each item links to its settings section. There's a progress ring (e.g. 3/5).

#### 4.2.2 Filter chips + sort
- **Quick chips** (single-row, horizontally scrollable on phone, 32 px tall, pill):

  | Chip | Filter |
  |---|---|
  | «Все выгодные» (default) | verdict in buy, maybe with action ≠ skip |
  | «Купить сразу» | `action=buy` |
  | «Торг» | `action=haggle` |
  | «Аукционы» | `action=bid` |
  | «Для себя» | `purpose=personal` |
  | «Рядом ≤ 10 км» | `max_km=10` |
  | «С доставкой» | shipping possible |
  | «Без ⚠» | no red flags |
  | «Не смотрел» | unseen |

  The action chips (Купить / Торг / Аукционы) are mutually exclusive; the others toggle. A selected chip is filled
  neutral-12 with inverted text and a leading check icon.
- **«⚙ Ещё фильтры»:** side sheet on desktop, bottom sheet on phone. Contains source (Kleinanzeigen/eBay), search
  (multi-select), price range (dual slider), min profit, min score (slider 0–100), status (incl. «Скрытые»), and
  «Найдено за» (1 ч / 24 ч / 7 дн / всё). A counter badge on the button shows how many filters are active. The footer
  has «Сбросить» and «Показать 34» (a live count).
- **Sort** (segmented on desktop, select on phone):

  | Label | Meaning |
  |---|---|
  | «Лучшие» | score desc, with a freshness boost |
  | «Новые» | first_seen desc |
  | «Прибыль» | expected_profit desc |
  | «Ближе» | distance asc |
  | «Скоро конец» | auctions `ends_at` asc; only visible with the Аукционы chip |

- **Persistence:** filter and sort state live in the hash query (shareable) and the last used set in `localStorage`.

#### 4.2.3 Deal card (grid, desktop and tablet) — anatomy
Container: surface-1, 1 px border, radius 16, overflow hidden. Hover lifts by 2 px (shadow-md), 160 ms.
The whole card is a button that opens the drawer.

```
┌──────────────────────────────┐
│[Покупай · 87]          (☆)   │  ← media 4:3, object-fit cover
│                              │     TL: action badge  TR: star toggle 32px circle
│                              │     BL: source chip «Kleinanzeigen» + «1/6» photos
│[KA] 1/6          ● 7 мин     │     BR: freshness chip (green dot if < 15 min)
├──────────────────────────────┤
│ iPhone 13 128GB Mitternacht, │  ← title 15/20 600, 2 lines clamp
│ Top Zustand mit OVP          │
│ 290 €  VB   рынок ~420 €     │  ← price 20/24 700 tnum; VB tag; market text-3
│ ┌──────────────────────────┐ │
│ │ ↗ +110 € прибыль · ROI 38%│ │  ← DECISION PILL (full width, tinted by action)
│ └──────────────────────────┘ │
│ 📍 4,2 км · Neukölln  ✓ ИИ   │  ← signals row 13px text-2, icons 14px
│ ⚠ Только самовывоз +1        │  ← max 1 red/amber flag chip + «+N»
├──────────────────────────────┤
│ [💬 Написать]  [☆]  [👁‍🗨 Скрыть]│  ← quick actions (desktop: shown on hover/focus;
└──────────────────────────────┘     touch devices: always shown)
```

- **Decision pill** (the most important element). Content by `Evaluation.action` and purpose:

  | Case | Pill text | Tint | Icon |
  |---|---|---|---|
  | buy (resale) | «+110 € прибыль · ROI 38 %» | green | `trending-up` |
  | haggle | «Предложи 300 € → +95 €» | amber | `hand-coins` |
  | bid (auction) | «Ставь до 260 € · 2 ч 14 мин» (live countdown; < 1 h → red text) | violet | `gavel` |
  | watch / maybe | «Подумай: мало данных о рынке» (first reason, trimmed) | neutral | `eye` |
  | personal | «Экономия 80 € (−19 %)» / «Предложи 480 € → экономия 90 €» | green / amber | `piggy-bank` |
  | free | «Бесплатно · рынок ~60 €» | green | `gift` |

- **Action badge** (top-left, over the photo): short verb + score. «Покупай · 87», «Торг · 74», «Аукцион · 81»,
  «Подумай · 58». White/ink glass chip (backdrop-blur 8 px, 80% surface) with a 6 px coloured dot, so photos of any
  colour stay readable.
- **AI status** in the signals row: «✓ ИИ» (blue, tooltip «Фото проверены: совпадают с описанием»), «⚠ Фото не
  проверены» (amber, when `ai_checked === false`), or nothing when AI is off.
- **Flags:** at most one chip visible. Red if it's a scam-type flag, amber for everything else. Full list in the drawer.
- **Status ribbon:** a starred card shows a filled yellow star. Contacted / bought cards show a small chip
  «Написал» / «Куплено» in place of the freshness chip.
- **Unseen:** a 6 px blue dot before the title until opened. Opened state is stored locally (`ebp.seen`, cap 5 000 ids),
  or server-side if available.
- **Images:** `loading="lazy"`, `referrerpolicy="no-referrer"`, placeholder on error. The media box shows a skeleton
  shimmer until `onload`.
- **Grid:** `grid-template-columns: repeat(auto-fill, minmax(272px, 1fr))`, gap 16 (20 at ≥ 1440).

#### 4.2.4 Deal row (phone default, plus desktop «компактно» view toggle)
- Horizontal layout: thumbnail 96 × 96 (radius 12) on the left with the action dot. On the right: title (2 lines,
  15/20), price row, decision pill (inline, auto width), and signals row (distance · time · AI).
- Minimum height 120 px. The whole row is tappable.
- Swipe gestures (should): left = «Скрыть» (with undo toast), right = «В избранное». Icons are revealed under the row
  and there is a threshold haptic (`navigator.vibrate(10)` if available).
- Desktop view toggle: `layout-grid` / `rows-3` icons in the toolbar.

#### 4.2.5 Quick actions
- **«Написать»:** copies the default German message (§7.4) with the suggested offer, opens the ad in a new tab and marks
  the status «Написал». Toast: «Сообщение скопировано — вставь его в чат продавцу · Отменить».
- **☆** toggles «Избранное».
- **«Скрыть»:** instant, then a toast «Скрыто · Отменить · Почему?». «Почему?» opens reason chips.
- **Overflow `ellipsis`:** «Открыть на Kleinanzeigen», «Скопировать ссылку», «Переоценить» (could).

#### 4.2.6 Live updates
- New matching deal arrives (SSE `deal.new`):
  - If the user is scrolled to the top, the card is inserted with a 1.2 s green glow ring.
  - Otherwise a sticky pill «↑ 2 новых» appears under the toolbar; clicking it scrolls to the top.
  - Toast: «Новая находка: iPhone 12 · +95 €», with an «Открыть» action.
  - The document title becomes «(2) Лента · EbeyParser» until the tab gets focus.
- Relative times re-render every 60 s. The auction countdown re-renders every 1 s, only for visible cards
  (IntersectionObserver).

#### 4.2.7 Pagination
Infinite scroll in pages of 30 (`/api/deals?limit=30&offset=`), with an explicit «Показать ещё» button as a fallback.
Restore the scroll position when coming back from the detail view.

### 4.3 Сделка: Deal detail
- **Desktop ≥ 1280:** right drawer 560 px wide. It doesn't block the list: the list stays scrollable, `j/k` moves the
  selection, and the drawer updates in place (triage pattern [S46]).
- **1024–1279:** modal drawer 520 px with a scrim (overlay 40%).
- **Phone:** a full-screen route with a sticky top bar (back, prev/next, share, «Открыть ↗») and a **sticky bottom
  action bar** (§4.3.12).
- The drawer header is sticky: close `x`, `chevron-up/down` (prev/next), `external-link` «Открыть объявление»,
  and `ellipsis`.

Section order, top to bottom:

#### 4.3.1 Gallery
- Main image 4:3 (radius 12 inside the drawer padding) with a counter «2 / 6» and arrow buttons on hover.
  Phone: swipe with CSS scroll-snap.
- Thumbnail strip: 56 px squares, horizontal scroll, the active one gets a 2 px ring.
- Click → full-screen lightbox: dark overlay, pinch or wheel zoom, `Esc` to close, arrow keys.
- AI annotation (should): if `ai.stock_photos === true`, an amber chip on the image: «Похоже на фото из интернета».

#### 4.3.2 Title block
- Title (h2 20/26).
- Price row: price (28/32 700 tnum) + «VB» tag + shipping text («+ 6,99 € доставка» / «только самовывоз»).
- Meta line: `map-pin` «4,2 км · 12047 Neukölln» · `clock` «7 мин назад» · source chip · search name chip.

#### 4.3.3 Decision block (hero card, tinted by action; 20 px radius, 20 px padding)
- **buy:**
  - Title: «**Покупай** — прибыль ≈ **+110 €**»
  - Sub: «Выгодно до **330 €** · ROI 38 % · цена в объявлении 290 €»
  - Primary button: «Написать продавцу». Secondary: «Открыть объявление ↗».
- **haggle:**
  - Title: «**Торгуйся: предложи 300 €**»
  - Sub: «По цене 360 € прибыль всего +35 €. При 300 € → **+95 €**. Дороже 330 € — не бери.»
  - **Offer slider** (should): range from 60% of the asking price up to `max_buy_price` + 10%. Default = `offer_price`.
    The live profit label recalculates client-side from the breakdown components. Past `max_buy_price` the track
    turns red: «уже невыгодно».
  - Primary: «Написать с предложением 300 €». The chosen value is passed to the composer.
- **bid:**
  - Title: «**Аукцион: ставь максимум 260 €**»
  - Sub: «Сейчас 180 € · 5 ставок · конец через **2:14:05** (сегодня в 21:30)»
  - Tip line (`info`): «Не повышай ставку раньше времени. Поставь свой максимум за 5–10 секунд до конца — eBay сам
    доторгуется за тебя.»
  - Primary: «Открыть на eBay ↗». Secondary: «Напомнить за 10 мин» (could: server schedules a Telegram ping).
- **watch / maybe:** neutral card. «**Подумай**: {first reason}». Up to 3 reasons listed.
- **skip:** neutral card. «Не выгодно: {reason}». Only reachable through filters.
- **personal:** «**Экономия 80 €** к рынку · твоя цель до 550 € ✓» (or «на 30 € дороже твоей цели — предложи 520 €»).
- **Unchecked banner** (amber, above the title when `ai_checked === false`):
  «⚠ Фото не проверены нейросетью — посмотри сам внимательно».

#### 4.3.4 Red flags (only if any; red-tinted card, above the numbers)
- Title: «Осторожно» with the `shield-alert` icon.
- List: flag text (from engine and AI, deduplicated) plus a one-line explanation mapped from known patterns:
  - «Vorkasse» → «Продавец хочет оплату заранее — без защиты покупателя. Только наличные при встрече или «Sicher bezahlen».»
  - «Nur Tausch» → «Только обмен, не продажа.»
  - iCloud / locked → «Устройство может быть заблокировано — проверь при встрече (§ чек-лист).»
  - stock photos → «Фото, похоже, из интернета — попроси сфотографировать с листком с сегодняшней датой.»
  - defekt → «Неисправно — прибыль посчитана как для исправного?»
- Unknown flags render as-is.

#### 4.3.5 Profit breakdown («Расчёт прибыли» / «Сколько сэкономлю»)
- A waterfall table built from the existing `profit_breakdown()`. Right-aligned tabular numbers; minus rows in text-2.

  | Row | Value |
  |---|---|
  | Рыночная цена (23 сравнения, 360–470 €) | 420 € |
  | − Запас на торг и риск (10 %) | −42 € |
  | − Комиссии при продаже (0 %) | 0 € |
  | − Доставка покупателю | 0 € |
  | − Цена покупки | −290 € |
  | **= Чистая прибыль** | **+88 €** |

  The total is bold, coloured by sign, with ROI under it.
- A tiny horizontal stacked bar visualises how much of the market price is left as profit.
- Toggle «при моём предложении 300 €» (when haggling) switches the buy-price row.
- Footer: «Источник рынка: своя история цен (60 дней)» + «Как считается?» popover.

#### 4.3.6 Market price history chart
- 160 px tall (desktop) / 140 (phone). Inline SVG, no chart library needed; see the `dataviz` guidance.
- **Data:** `GET /api/deals/:id/market` (new) returns `price_points` for this product key:
  `[{price, seen_at, source, sold, url}]` plus `median`, `p25`, `p75`, and `max_buy_price`.
- **Marks:**
  - Dots: asking prices, 3 px, text-3 at 60% opacity. eBay-sold dots are filled blue.
  - Median: 1.5 px line, text-2.
  - p25–p75: band, neutral-4 fill.
  - «Выгодно до 330 €»: dashed green line with an end label.
  - **This ad:** 6 px dot in the action colour, with a label «290 €».
- **Range tabs** above the chart: «30 дн · 60 дн · Всё» [S33].
- **Tooltip** on hover or tap: «14.09 · 399 € · Kleinanzeigen», with click-through to the comparable.
- **Caption** below: «Рынок ~420 € по 23 объявлениям за 60 дней · держатся в среднем 5 дней».
- **Empty state:** «Истории цен ещё мало — оценка по похожим объявлениям» (the chart is hidden and the comparables list
  is shown instead).

#### 4.3.7 Comparables («Похожие предложения»)
- List of up to 5 rows: title (1 line), price (right, tnum), source chip, «продано» tag for sold items, date.
  Each row is a link. The ad's own price is shown as a reference row highlighted in the action tint.
- «Показать все 23» expands the list.

#### 4.3.8 AI findings («Что увидела нейросеть»)
- Header: `scan-eye` + model name (mono 12) + verdict chip + response time if known.
- Key-value grid (2 columns on desktop):

  | Key | Value |
  |---|---|
  | Товар | e.g. «NVIDIA GeForce RTX 3090 24GB» |
  | Состояние | «Хорошее» |
  | Фото = описание | ✓ Да / ✗ Нет / ? Неясно (coloured) |
  | Блокировка | «не видно» / «⚠ возможна» |
  | Фото из интернета | «нет» / «⚠ похоже» |
  | Дефекты | chips |
  | Уверенность | bar + % |

- The reasoning text is set as a quote (blockquote, 3 px left border in blue).
- «Второе мнение (Claude)» is collapsible when `ai_second` is present.

#### 4.3.9 Seller & listing signals
- Chips: «Частный продавец» / «Коммерческий» (amber: «у коммерческих нет торга, зато есть гарантия»),
  «Versand möglich», «Direkt kaufen», eBay feedback «99,2 % · 1 204 отзыва», and condition.
- **«Безопасная сделка»** tip block (always, collapsed to 1 line on repeat views):
  «Плати наличными при встрече или через «Sicher bezahlen». Не переходи в WhatsApp, не плати «PayPal Freunde»,
  не открывай ссылки на оплату из сообщений.» [S25, S26]

#### 4.3.10 Checklist «Проверь при встрече»
Category-aware (§7.5). Checkboxes are stored in the note. Button «Скопировать чек-лист».

#### 4.3.11 Message composer («Сообщение продавцу»)
- **Template tabs** (pills):
  - «Предложить цену» (default when an offer is known)
  - «Купить по цене»
  - «Спросить наличие»
  - «Уточнить состояние»
  - «Доставка»
- **Variables:** `{offer}` (from the decision/slider), `{day}` (dropdown «heute / morgen / am Wochenende»), and
  «наличными» (toggle, default on).
- **Textarea** with the German text (editable, 16 px on phone). Below it, a muted Russian translation that updates live
  for unedited templates. Once edited, it shows «перевод недоступен после правки».
- **Buttons:** «Скопировать» (primary) · «Скопировать и открыть объявление» (secondary).
  After copying: toast «Скопировано» + an offer to set the status to «Написал».
- **Etiquette hint** (text-3, 12 px): «Один вежливый торг — ок. Повторные попытки сбить цену продавцы не любят.» [S20]

#### 4.3.12 Status actions (desktop: inline after the composer; phone: **sticky bottom bar**)
- Phone bar: `[☆]` `[Написал]` `[Купил]` `[⋯]`, with the primary context action on the right (e.g. «Написать»).
- Pipeline stepper: «Избранное → Написал → Купил → Продал». The current step is filled; clicking a later step moves the
  deal there.
- **«Купил»** opens a small dialog: «За сколько купил?» (prefilled with offer or price) + date (today). Saved to the
  pipeline.
- **«Продал»** opens: «За сколько продал?» (prefilled with market), extra costs (fees/shipping, prefilled from pricing
  settings) → shows real profit.
- **«Скрыть»** with reason chips.
- **Notes:** textarea «Заметка», autosaved on blur (debounced 800 ms), with a «Сохранено ✓» micro-label.

#### 4.3.13 Why this score («Почему такая оценка»)
Collapsed list of `reasons[]`, shown at the end for power users.

### 4.4 Мои сделки: My deals pipeline
- **Summary tiles** at the top: 4 tiles (desktop, 4-up) / 2 × 2 (phone). Stat tile spec §6.8.13.

  | Tile | Example |
  |---|---|
  | «Заработано за месяц» | **+310 €**, with a sparkline over 6 months |
  | «Вложено сейчас» | **640 €** in 3 items |
  | «Ожидаемая прибыль в наличии» | **+210 €** |
  | «Точность прогноза» | «факт в среднем −12 % от прогноза». Tooltip explains; shown after ≥ 3 sold deals. |

- **Desktop:** 4 kanban columns — **Избранное · Написал · Купил · Продал**.
  - Column header: name + count + sum (e.g. «Купил · 3 · 640 €»).
  - Cards are compact (thumbnail 48, title, key number) and can be dragged between columns (should; buttons are the
    must). Dropping into «Купил» or «Продал» opens the price dialog.
  - «Скрытые» and «Не купил» (lost deals) are at the bottom as a collapsible archive link.
- **Phone:** segmented control with the 4 columns + counts, and a list below.
- **Card content by column:**

  | Column | Content |
  |---|---|
  | Избранное | price · decision pill · freshness (ads older than 3 days show «может быть уже продано»); action «Написать» |
  | Написал | «написал 2 ч назад» · offer sent; after 24 h an amber «Продавец ответил?» nudge with «Купил» / «Не вышло» |
  | Купил | paid price · «в наличии 6 дней» · target sell price (= market) · action «Продал»; items held > 21 days get an amber «залежалось» chip [S32] |
  | Продал | bought → sold · **real profit** (green/red) · predicted vs actual line «прогноз +110 € · факт +95 €» · days to sell |

- **Export:** «Скачать CSV» (could) for the user's own spreadsheet.
- **Empty states:** see §5.

### 4.5 Поиски: Searches manager
- **Header:** title «Поиски», sub «Что и где я ищу. Выключенные поиски не тратят запросы.», primary button «+ Новый поиск».
- **Load meter bar** (full width, top): «Нагрузка на Kleinanzeigen: ~64 из 150 страниц в час · проверка каждые 30 мин»
  in green / amber / red. «Как это работает?» opens a popover explaining ban protection.
- **Search cards** (grid 2–3 columns desktop, 1 on phone). Anatomy:
  - 40 px icon tile (category icon, `search` for keywords, `link` for URL, `gavel` for eBay, `heart` for wishlist).
  - Name (16/600) and a toggle switch (on/off) on the right.
  - Chips line: «Berlin · 30 км», «50–400 €», «Перепродажа» / «Для себя до 550 €», «Kleinanzeigen» / «eBay».
  - Stats row (tnum): «Новых за 24 ч **86**» · «Выгодных за 7 дн **4**» · «Последняя проверка 8 мин назад».
  - State badge:
    - «Обучение» (blue, `hourglass`, tooltip «первая проверка только изучает цены»)
    - «Активен» (green)
    - «Выключен» (neutral)
    - «Ошибка» (red, with the message: «Kleinanzeigen не отвечает — пауза до 14:10»)
    - «Не проверен» (neutral)
  - Overflow menu: «Изменить», «Дублировать», «Показать находки» (feed filtered to this search), «Удалить» (confirm dialog).
- **Grouping:** «Категории в районе», «По словам», «Для себя», «eBay». Section headers only render when a group has more than 0 items.
- **«+ Новый поиск»** opens a chooser (modal on desktop, sheet on phone) with 5 big options:

  | Option | Icon | Line |
  |---|---|---|
  | «Категория в моём районе» | `layout-grid` | «Все новые объявления категории» |
  | «По словам» | `search` | «Например: steam deck, rtx 3080» |
  | «Вставить ссылку с Kleinanzeigen» | `link` | «Настрой фильтры на сайте и вставь адрес — я разберу его сам» |
  | «Для себя» | `heart` | «Что ищешь и до какой цены» |
  | «eBay» | `gavel` | «Аукционы и Sofort-Kaufen» (disabled with «Сначала подключи eBay» if no keys) |

- **Edit side panel** (drawer 480 px; full screen on phone) with sections:
  1. **Что:** name (auto-generated, editable, e.g. «Handy & Telefon · Berlin 30 км»); category picker (same cards as
     onboarding, single select) or keywords input, or URL (paste → server parses →
     «Распознал: категория Notebooks, Berlin, 20 км, 100–500 €»).
  2. **Где:** city autocomplete + radius slider (as in onboarding).
  3. **Цена:** min / max price (dual slider + inputs); for «Для себя»: «Сколько готов заплатить» (target_price) + «показывать до» (max_price).
  4. **Фильтры:** «Исключить слова» chip input with suggestions (defekt, bastler, suche, tausch, ersatzteil, «nur
     Tausch») and «Обязательные слова».
  5. **Порог выгоды** (optional overrides): «Свой минимум прибыли / ROI», with an «использовать общие» toggle.
  6. **eBay only:** type (Sofort-Kaufen / Аукцион / Preisvorschlag), condition, «Только самовывоз в радиусе»,
     «Аукционы, которые заканчиваются в ближайшие N ч».
- **Panel footer:** «Отмена» / «Сохранить». Validation errors are inline per field (from `search_problems()`).
  On save: toast «Поиск сохранён · первая проверка — обучение».

### 4.6 Состояние: Health
- **Top banner** (one sentence + fix CTA):
  - all ok: green «Всё работает. Последняя проверка 8 мин назад: 212 новых, 3 выгодных.»
  - problem: amber / red «Нейросеть не отвечает с 13:05 — уведомления идут с пометкой «фото не проверены».
    [Проверить нейросеть]»
- **Status tiles** (grid 3 × 2 desktop, 1 column phone). Each tile: icon, name, state line, 1–2 metrics, action button.

  | Tile | Metrics | Actions |
  |---|---|---|
  | **Проверки** | «Работает · следующая через 12:03» (live), interval, current run progress «поиск 3 из 8» | «Проверить сейчас», «Пауза / Продолжить» |
  | **Нейросеть** | server + model, «ответ ~14 с», AI calls last run, queue | «Проверить», «Сменить модель» → Settings |
  | **Kleinanzeigen** | requests/hour meter `64 / 150`, «работает» / «пауза до 14:10 (блокировка №2)» with countdown, last block reason | «Снизить нагрузку» → interval settings |
  | **eBay** | «Подключён · 312 / 5 000 запросов сегодня» or «Не подключён» | «Подключить» |
  | **Уведомления** | per channel: Telegram ✓ «последнее 11:42», Email off; failed deliveries count; «в очереди 4» (rate cap) | «Отправить тест» |
  | **Очередь оценки** | «ждут оценки 37 · просрочено за 24 ч 2» | tooltip explains the funnel budgets |

- **Runs timeline** («Последние проверки»):
  - Desktop: a table. Columns: time, duration, new, evaluated, deals, notified, errors (a red count opens details).
  - Phone: a card list.
  - Above the table, a sparkline/bar chart «выгодных за проверку» over the last 48 runs.
- **Logs viewer** (`#/health/logs`):
  - Monospace 12/18 on surface-2.
  - Level filter chips: «Ошибки», «Предупреждения», «Всё».
  - Text search; «Следить» toggle (auto-scroll, SSE tail); «Скопировать» (last 200 lines); «Скачать лог».
  - Lines colour-coded by level (red / amber / text-2).
  - Timestamps in Berlin time.
  - Backend: `GET /api/logs?level=&q=&limit=500` and `GET /api/logs/stream`.
- **Heartbeat hint:** «Каждый день в 9:00 пришлю отчёт «жив». Нет отчёта — значит, компьютер или программа выключены.»

### 4.7 Настройки: Settings
- **Layout:** desktop has a left sub-nav (200 px list) + content (max 720 px). Phone has a list of sections → section page.
- Each section is a stack of **setting groups**: a card with a title, a 1-line description, then rows.
- **Row layout:** label + helper on the left; control on the right. On phone, the control goes below.
- **Save model:** per-section **sticky save bar** appears when dirty: «Есть несохранённые изменения · [Отменить] [Сохранить]».
  - It animates up from the bottom (200 ms).
  - Leaving the section with unsaved changes → confirm «Уйти без сохранения?».
  - After save: the bar turns green «Сохранено ✓» for 2 s.
  - If a change needs a monitor restart, the backend does it and the bar shows «Применяю…».
- **Sections:**
  1. **Поиск и регион** (`map-pin`):
     - Default city and radius (used for new searches).
     - Check interval: slider 10–120 min with the load meter and a warning if too frequent.
     - «Первая проверка — только обучение» (toggle, recommended on).
     - «Не смотреть объявления дешевле» (€).
     - «Всегда исключать слова» (global chip input).
     - Advanced disclosure: pages per search, pause between requests, limits per run (details/comps/AI), hourly cap,
       cooldown steps. Each has a helper and a «по умолчанию» reset link.
  2. **Деньги** (`wallet`):
     - Strategy presets (same 3 cards as onboarding).
     - Min profit, min ROI, risk margin.
     - «Максимум на одну вещь» (max_capital).
     - Fees: selling %, payment %, PayPal fixed fee.
     - «Моя доставка покупателю» (€).
     - «Скидка при VB» (%).
     - «Сколько сравнений нужно для уверенной оценки».
     - «Справочник цен»: a table editor (keywords chips + price + exclude), with the warning «Заполняй только если
       точно знаешь рынок».
     - A live example box, as in onboarding.
  3. **Нейросеть** (`scan-eye`):
     - Status card with «Проверить» and «Проверить на примере».
     - Provider (auto-detected; «LM Studio» / «Ollama» / «Другой сервер» → URL field).
     - Model dropdown (refresh icon).
     - «Фото на объявление» (1–5 segmented).
     - «Ждать ответ до» (s).
     - «Уменьшать фото до» (px).
     - «Проверять» (только перспективные / все).
     - **Второе мнение (Claude)** group: toggle, API key (masked), «проверять только балл от» slider,
       «не больше N за проверку». Cost warning: «платно, по твоему ключу».
  4. **Уведомления** (`bell`):
     - Channels: Telegram (connected card: «@maks_deals_bot → Maksim», buttons «Тест», «Переподключить», «Отключить»);
       e-mail (guided setup as in onboarding).
     - «Что присылать»: verdict chips (Покупать / Подумать), min score slider with a **live preview**
       «за последние 7 дней пришло бы 14 уведомлений» (should; `GET /api/notify/preview?min_score=&verdicts=`).
     - «Как присылать»: instant / digest.
     - «Не больше N в час».
     - «Служебные сообщения» (toggle, «нейросеть упала, сайт заблокировал»).
     - «Утренний отчёт в» (time select / off).
     - «Присылать, даже если фото не проверены» (toggle).
  5. **eBay** (`gavel`): keys (masked) + «Проверить», limit usage, marketplace (EBAY_DE), «Искать только в Германии» (DE).
  6. **Доступ с телефона** (`smartphone`):
     - **Mode** (3 radio cards):
       - «Только этот компьютер» (default, `127.0.0.1`)
       - «Через Tailscale (рекомендуем)» — asks for the 100.x address or auto-detects Tailscale interfaces
       - «Домашний Wi-Fi» (`0.0.0.0` + token), with a red warning «Не включай в общежитии/кафе»
     - A mode change opens a confirm dialog explaining the risk.
     - **QR code** with the phone link including the token (rendered client-side, vendored tiny QR lib), plus
       «Скопировать ссылку».
     - «Сменить ключ доступа» (confirm: «Все телефоны нужно будет подключить заново»).
     - «Дополнительные имена» (allowed_hosts chips).
  7. **Данные и бэкап** (`database`):
     - DB size and counts («объявлений 12 408 · цен в истории 38 112»).
     - «Хранить объявления N дней».
     - «Скачать резервную копию» (zip of database + settings; secrets excluded unless «включить ключи» is checked,
       with a warning).
     - «Восстановить из копии» (upload + confirm).
     - «Экспорт сделок в CSV».
     - Danger zone (red border):
       - «Сбросить историю цен» (typed confirmation)
       - «Удалить все данные» (typed «удалить»)
       - «Запустить мастер настройки заново»
     - «Для продвинутых» disclosure: config file path, `.env` path, version, «Открыть папку данных» (could).
  8. **О программе** (`info`): version, links to README, «Проверить обновления» (could), and the licence of vendored libs.

### 4.8 Проверить объявление (modal)
- Input «Вставь ссылку на объявление Kleinanzeigen или eBay», purpose toggle «Перепродажа / Для себя», and for
  personal an optional «Хочу заплатить до».
- «Оценить» → a progress list with checkmarks as each stage completes (SSE or polling):
  «Открываю объявление… → Ищу цены похожих… → Нейросеть смотрит фото… (~15 с)».
- Result: the same decision block, red flags and breakdown as §4.3. Buttons: «Открыть полностью» (routes to
  `#/deal/:id`) and «В избранное».

---

## 5. States matrix (every screen)

| Screen | Loading | Empty | Error | Partial / offline |
|---|---|---|---|---|
| Onboarding AI | skeleton card «Ищу LM Studio и Ollama…» | variant C (install guide) | «Не смог проверить: {human msg}» + «Ещё раз» | — |
| Onboarding Telegram | inline spinner in button, «Жду сообщение…» with 2 min countdown | — | per sub-step inline error | «Нет интернета — Telegram недоступен. Проверь подключение.» |
| Лента | 6 skeleton cards (media block + 3 text bars), hero skeleton | see strings below (4 variants) | card «Не удалось загрузить ленту» + «Обновить» | top banner «Нет связи с программой — показываю то, что было в 12:31» (grey), auto-retry with backoff 2→30 s |
| Сделка | skeleton gallery + blocks | «Объявление не найдено — возможно, удалено из базы» + «В ленту» | per-section soft errors (chart failed → caption «График недоступен») | — |
| Мои сделки | skeleton columns | per column hint (below) | toast + retry | — |
| Поиски | skeleton cards | «Поисков нет» + big «Настроить за 3 минуты» (→ onboarding) + «Добавить вручную» | inline on card (red badge) | read-only banner if config not writable: «Не могу сохранить настройки: нет доступа к файлу. [Подробнее]» |
| Состояние | tile skeletons | «Проверок ещё не было — первая начнётся через 1 мин» | tiles show their own error state | — |
| Настройки | form skeleton | — | per-field validation; save failure → bar turns red «Не сохранилось: {reason}» + «Ещё раз» | — |

**Feed empty-state variants:**
1. **No searches:** illustration `radar` + «Здесь появятся выгодные находки» + «Сначала расскажи, что искать — это 3 минуты» + primary «Настроить» + secondary «Посмотреть демо».
2. **Learning** (first pass running, nothing yet): `hourglass` + «Изучаю рынок в твоём районе» + progress + «Первые находки — после следующей проверки, примерно в 12:40. Уведомлю в Telegram.»
3. **Filters too tight:** `sliders-horizontal` + «По этим фильтрам ничего» + «Сбросить фильтры» + «Показать все».
4. **Quiet day** (running, 0 deals in 24 h): `coffee` + «Сегодня выгодного пока не было» + «Обычно 2–5 находок в день. Можно расширить радиус или добавить категории.» + «Изменить поиски».

**Pipeline column hints:**
- Избранное: «Нажимай ☆ на находках — они соберутся здесь»
- Написал: «Когда напишешь продавцу — отметь «Написал», я напомню проверить ответ»
- Купил: «Отмечай покупки — посчитаю, сколько вложено»
- Продал: «Здесь будет твоя реальная прибыль»

**Loading timing rules** [S47]:
- Nothing for 0–300 ms.
- An inline spinner for atomic actions (button shows a spinner and keeps its width).
- Skeletons for content areas expected to take more than 300 ms.
- Any wait over 5 s shows a text label of what is happening.

---

## 6. Visual design system

Direction: **"calm money app"**. It borrows the dense, quiet chrome of Linear/Raycast [S41, S43], the friendly rounded
consumer feel of Wise/Revolut [S48, S49], and the photo-led cards of Airbnb/Vinted [S50].
Rules: the neutral UI steps back, and colour appears only where there is money or danger.
Dark and light are equal; `prefers-color-scheme` is the default, with a manual toggle.

### 6.1 Color tokens
Structure follows the Radix step roles [S51]: subtle background → border → solid → text. Define everything as CSS
custom properties on `:root` and override in `[data-theme="dark"]` (and in `@media (prefers-color-scheme: dark)` when
no manual choice has been made). Dark mode avoids pure black, lifts elevated surfaces lighter, and uses slightly
desaturated accents [S52].

**Neutrals**

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | `#F6F7F9` | `#0B0D10` | app background |
| `--surface-1` | `#FFFFFF` | `#12151A` | cards, drawer |
| `--surface-2` | `#F1F3F6` | `#181C22` | sidebar, inputs, nested panels |
| `--surface-3` | `#E8EBEF` | `#20252D` | hover on surface-1, pressed chips |
| `--overlay` | `rgba(14,17,22,.40)` | `rgba(0,0,0,.60)` | scrim |
| `--border` | `#E3E6EA` | `#262C34` | hairlines (1 px) |
| `--border-strong` | `#CDD2D9` | `#343B45` | inputs, focusable outlines |
| `--text` | `#0E1116` | `#ECEFF3` | primary text |
| `--text-2` | `#4A5361` | `#A9B1BC` | secondary |
| `--text-3` | `#6B7480` | `#7A8491` | captions, placeholders (≥ 4.5:1 on surface-1 in light) |
| `--text-disabled` | `#A3AAB4` | `#4B535E` | disabled |
| `--ink` | `#0E1116` | `#F2F4F7` | neutral solid (selected chips, secondary emphasis) |
| `--on-ink` | `#FFFFFF` | `#0B0D10` | text on ink |

**Semantic hues.** Each hue has 5 tokens: `-bg` (tinted fill), `-border`, `-solid`, `-text` (on bg/surface), `-on-solid`.

| Hue → meaning | Token prefix | Light bg / border / solid / text / on-solid | Dark bg / border / solid / text / on-solid |
|---|---|---|---|
| **Green** → profit, buy, success, **brand** | `--green` | `#E9F9EF` / `#B7EBC8` / `#22C55E` / `#15803D` / `#052E16` | `#0F2419` / `#1C4A2E` / `#2BD46A` / `#4ADE80` / `#052E16` |
| **Amber** → haggle, offer, warning | `--amber` | `#FFF6E0` / `#F7D58A` / `#F5A524` / `#A2560A` / `#3B2300` | `#291E08` / `#5C4210` / `#F5A524` / `#FBBF24` / `#3B2300` |
| **Violet** → auction, bid | `--violet` | `#F1EEFE` / `#D5CCFB` / `#7C5CFA` / `#5B3FD9` / `#FFFFFF` | `#1D1836` / `#3D3372` / `#8B7CF6` / `#B4A8FF` / `#FFFFFF` |
| **Red** → danger, scam, loss, destructive | `--red` | `#FDECEC` / `#F5C2C2` / `#E5484D` / `#C62A2F` / `#FFFFFF` | `#2B1214` / `#5A2226` / `#E5484D` / `#FF8B8B` / `#FFFFFF` |
| **Blue** → info, AI, links, focus | `--blue` | `#EAF3FF` / `#BCD8FF` / `#3B82F6` / `#1D5FD1` / `#FFFFFF` | `#0F1D33` / `#1E3A66` / `#4C8DF6` / `#7DB2FF` / `#FFFFFF` |

- **Primary button** = `--green-solid` background with `--green-on-solid` (very dark green) text. This is the
  Wise-style lime pattern [S48]: accessible (≈ 8:1) and on-brand with the green logo. Don't put white text on green
  (#16A34A + white fails AA for 14 px).
- **Focus ring:** `0 0 0 2px var(--bg), 0 0 0 4px var(--blue-solid)` on every focusable element (`:focus-visible` only).
- **Links:** `--blue-text`, underline on hover.
- **Verdict → hue mapping** (single source of truth, `verdictTone()` in JS):

  | Case | Hue |
  |---|---|
  | buy | green |
  | haggle | amber |
  | bid | violet |
  | maybe / watch | neutral (`--surface-3` bg, `--text-2`) |
  | skip | neutral, 60% opacity card |
  | red flags | red |
  | AI info | blue |
  | personal savings | green (the icon `piggy-bank` distinguishes it) |

- **Money signs:** positive `+110 €` in `--green-text`, negative `−35 €` in `--red-text` (true minus sign U+2212), zero in `--text-2`.

### 6.2 Typography
- **Font:** **Inter** variable (OFL, supports Cyrillic and German umlauts), vendored at
  `/static/fonts/InterVariable.woff2` with `font-display: swap`. Fallback stack:
  `system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans", sans-serif`.
  Headings may use `Inter Display` optical size via `font-variation-settings: "opsz" 32` if present [S41].
- **Numbers:** `font-variant-numeric: tabular-nums` on all prices, stats, tables, countdowns [S53, S54].
  Use the class `.num` or apply it globally to `[data-num]`.
- **Mono:** `ui-monospace, "JetBrains Mono", SFMono-Regular, Consolas, monospace`. Use only for model names, tokens and logs.

| Token | Size / line-height | Weight | Tracking | Use |
|---|---|---|---|---|
| `display` | 32 / 38 (phone 28/34) | 700 | −0.02em | hero numbers, onboarding titles |
| `h1` | 24 / 30 | 650 | −0.015em | page titles |
| `h2` | 20 / 26 | 600 | −0.01em | section titles, drawer title |
| `h3` | 16 / 22 | 600 | −0.005em | card titles, group titles |
| `body-lg` | 16 / 24 | 400 | 0 | onboarding text, phone inputs |
| `body` | 15 / 22 | 400 | 0 | default UI text (desktop) |
| `body-sm` | 13 / 18 | 400 | 0 | meta rows, helpers, table cells |
| `caption` | 12 / 16 | 500 | +0.01em | chips, badges, timestamps |
| `overline` | 11 / 14 | 600 | +0.06em, uppercase | section eyebrows («СЕГОДНЯ») |
| `price-xl` | 28 / 32 | 700 | −0.02em, tnum | deal detail price |
| `price-lg` | 20 / 24 | 700 | −0.01em, tnum | card price |
| `mono-sm` | 12 / 18 | 400 | 0 | logs, model ids |

Rules:
- Hierarchy comes from size and weight, not colour.
- At most 3 sizes per card.
- Russian text runs about 15–25% longer than English: never fix button widths.
- Use `hyphens: auto` on `lang="ru"` body text.
- Keep `text-wrap: balance` for headings.

### 6.3 Spacing, layout, breakpoints
- **Spacing scale (4-based):** `--s-0: 0`, `--s-1: 2px`, `--s-2: 4px`, `--s-3: 8px`, `--s-4: 12px`, `--s-5: 16px`,
  `--s-6: 20px`, `--s-7: 24px`, `--s-8: 32px`, `--s-9: 40px`, `--s-10: 48px`, `--s-11: 64px`.
  (Linear/Stripe/Raycast all use 4/8-based scales [S55, S56, S43].)
- **Card padding:** 16 (phone) / 20 (desktop). Drawer padding 24. Page gutter: 16 phone, 24 tablet, 32 desktop.
- **Content max width:** 1440 (feed grid), 960 (onboarding card), 720 (settings forms).
- **Breakpoints:** `sm < 600` (phone) · `md 600–1023` (tablet, rail) · `lg 1024–1279` (sidebar + modal drawer) ·
  `xl ≥ 1280` (sidebar + non-modal drawer) · `2xl ≥ 1600` (feed 5 columns) [S42].
- **Touch targets:** ≥ 44 × 44 px on touch devices (`@media (pointer: coarse)`); desktop controls may be 32–36 px.

### 6.4 Radii

| Token | Value | Use |
|---|---|---|
| `--r-xs` | 6px | tags (VB), keycaps, small badges |
| `--r-sm` | 8px | small buttons, inputs (desktop dense) |
| `--r-md` | 12px | buttons, inputs, thumbnails, images inside cards |
| `--r-lg` | 16px | cards, popovers, tiles |
| `--r-xl` | 20px | drawers, decision block, bottom-sheet top corners, onboarding card |
| `--r-full` | 9999px | chips, pills, toggles, avatars, badges on photos |

### 6.5 Elevation / shadows
Light mode uses soft layered shadows. Dark mode uses the surface ladder + border, and shadows only on floating layers [S43, S52].

| Token | Light | Dark |
|---|---|---|
| `--shadow-sm` (cards at rest) | `0 1px 2px rgba(16,24,40,.05)` | `none` (border only) |
| `--shadow-md` (card hover, sticky bars) | `0 1px 2px rgba(16,24,40,.06), 0 8px 24px -8px rgba(16,24,40,.14)` | `0 8px 24px -8px rgba(0,0,0,.6)` |
| `--shadow-lg` (drawers, popovers, sheets) | `0 2px 6px rgba(16,24,40,.06), 0 24px 48px -12px rgba(16,24,40,.22)` | `0 24px 48px -12px rgba(0,0,0,.7)` + 1px `--border-strong` |
| `--shadow-toast` | `0 12px 32px -8px rgba(16,24,40,.28)` | `0 12px 32px -8px rgba(0,0,0,.8)` |

**Z-index:** sticky toolbar 10 · sidebar 20 · topbar 30 · drawer 40 · bottom sheet 45 · modal 50 · toast 60 · tooltip 70.

### 6.6 Motion
- **Durations:** `--d-fast` 120 ms (hover, press, colour) · `--d-base` 180 ms (toggles, chips, tabs) ·
  `--d-enter` 240 ms (drawers, sheets, step transitions) · `--d-exit` 180 ms (exits are faster than entries).
- **Easing:** `--ease-standard: cubic-bezier(.2,.8,.2,1)`; `--ease-enter: cubic-bezier(.05,.7,.1,1)`;
  `--ease-exit: cubic-bezier(.3,0,.8,.15)`.
- **Microinteractions:**
  - Button press: `transform: scale(.98)` for 120 ms.
  - Star toggle: scale 1 → 1.25 → 1 plus a fill colour change.
  - Toggle thumb slides 180 ms.
  - Copied state: the icon swaps to `check` for 1.5 s.
  - New deal glow: `box-shadow` ring in `--green-solid` at 40% alpha, fading over 1.2 s.
  - Hero numbers count up once per page load (≤ 600 ms).
  - Countdowns change without animation (tnum prevents jitter).
- Drawer enters with `translateX(24px)` + fade; bottom sheet with `translateY(100%)`. Reduced motion: opacity only, ≤ 100 ms.
- `@media (prefers-reduced-motion: reduce)`: disable glow pulses, count-ups, shimmer (static skeleton), and
  swipe-reveal animations.
- Skeleton shimmer is a 1.4 s linear gradient sweep, `--surface-2` → `--surface-3`.

### 6.7 Layout patterns
- App shell: CSS grid `grid-template-columns: var(--sidebar-w, 240px) 1fr` (xl adds `560px` for the open drawer).
- Sticky topbar with `backdrop-filter: saturate(180%) blur(12px)` over a 85% `--bg`.
- Bottom tab bar: fixed, `padding-bottom: env(safe-area-inset-bottom)`, surface-1 with a top border.
  Content gets `padding-bottom: calc(64px + env(safe-area-inset-bottom) + 16px)`.

### 6.8 Components

#### 6.8.1 Buttons

| Variant | Background / text | Use |
|---|---|---|
| primary | `--green-solid` / `--green-on-solid` | one per view: «Дальше», «Написать продавцу», «Сохранить» |
| secondary | `--surface-1` + 1 px `--border-strong` / `--text` | «Открыть объявление», «Проверить» |
| ghost | transparent / `--text-2` → hover `--surface-3` | «Назад», «Пропустить», toolbar actions |
| danger | `--red-solid` / white | confirm dialogs only |
| danger-ghost | transparent / `--red-text` | «Удалить поиск» in menus |
| tinted | `--{hue}-bg` / `--{hue}-text` | contextual (e.g. amber «Предложить 300 €» inside the haggle block) |

- **Sizes:** `sm` 32 px (padding 0 12, 13/600), `md` 40 px (0 16, 14/600), `lg` 48 px (0 20, 16/600; phone
  primary, onboarding). Icon 16/18/20 with gap 8. Radius `--r-md`; the `lg` phone full-width uses `--r-lg`.
- **Icon button:** square 32/40, radius `--r-md`, with a tooltip (desktop) and `aria-label`.
- **States:**

  | State | Treatment |
  |---|---|
  | hover | bg shift one step |
  | active | scale .98 |
  | focus-visible | ring |
  | disabled | 45% opacity, `cursor: not-allowed`, and a tooltip saying why |
  | loading | spinner replaces the icon, label stays, width locked, `aria-busy` |

#### 6.8.2 Inputs
- Height 40 (desktop) / 48 (phone). Padding 0 12. `--surface-2` bg, 1 px `--border-strong`, radius `--r-md`,
  font 15 desktop / **16 phone**.
- Focus: border `--blue-solid` + ring.
- Invalid: border `--red-solid` + message under the field (13, `--red-text`, `circle-alert` icon).
- Label above (13/600 `--text-2`), helper below (13 `--text-3`).
- Suffix slot for units («€», «км», «мин», «%»). Number inputs use `inputmode="decimal"` and accept a comma decimal
  («19,99»).
- Secret input: masked, with a `eye` / `eye-off` toggle; stored secrets show as «сохранён ••••x9» and a «Заменить» button.
- **Chip input** (exclude words): tokens as 28 px chips with `x`; Enter or comma adds; paste splits by comma/newline.

#### 6.8.3 Toggle (switch)
Track 36 × 20 (desktop) / 44 × 26 (phone), thumb 16/22, radius full. Off: `--surface-3` track. On: `--green-solid`
track, white thumb. Label on the left, the switch on the right. `role="switch"` with `aria-checked`.

#### 6.8.4 Slider
- Track 4 px `--surface-3`; the filled part is `--ink` (or the hue in the offer slider).
- Thumb 20 px white with `--shadow-md` and a 1 px border; 28 px hit area.
- Value bubble above the thumb while dragging (caption, tnum).
- Discrete stops show 4 px ticks.
- Keyboard: arrows step, PgUp/PgDn ×5.
- Paired number input where precision matters (budget, prices).

#### 6.8.5 Chips
- **Filter chip:** 32 px, padding 0 12, radius full. Default: `--surface-1` + 1 px border, `--text-2`. Selected:
  `--ink` bg, `--on-ink` text, leading `check` 14. Optional count «Торг 4».
- **Info chip / tag:** 22–24 px, caption 12/500, radius full, `--{hue}-bg` / `--{hue}-text`.
- **VB tag:** 20 px, radius `--r-xs`, `--amber-bg` / `--amber-text`, 11/700 «VB».

#### 6.8.6 Badges
- **Action badge on photo:** glass chip (surface-1 at 82% + blur 8), 26 px, dot 6 px in the hue, text 12/600.
- **Decision pill:** 36 px (card) / auto-height in the decision block; radius `--r-md`; `--{hue}-bg` background,
  1 px `--{hue}-border`, `--{hue}-text` text 14/650 tnum; leading 16 px icon.
- **Count badge (nav):** min-width 18, height 18, radius full, `--red-solid` / white 11/700 (or `--blue-solid` for
  «новые»).
- **Status dot:** 8 px; running = green + 2 s pulse ring; warning = amber; error = red; paused = `--text-3`.

#### 6.8.7 Cards
- Base: `--surface-1`, 1 px `--border`, radius `--r-lg`, `--shadow-sm`, padding 16/20.
- Interactive cards: hover `--shadow-md` + `translateY(-2px)`; focus ring.
- Selected (e.g. category or preset card): `--green-bg` background, 1.5 px `--green-solid` border, top-right filled
  `circle-check` 20 px.

#### 6.8.8 Drawer (desktop) / Bottom sheet (phone)
- **Drawer:** right, 480–560 px, `--surface-1`, `--shadow-lg`, left radius `--r-xl` (flush on xl non-modal), sticky
  header 56 px with a bottom border. Traps focus when modal. `Esc` closes; the scrim click closes.
- **Bottom sheet:** top radius `--r-xl`, grabber 36 × 4, max-height 90 dvh, drag to dismiss (should), snap points
  50% / 90%, safe-area padding. Use it for filters, the «Новый поиск» chooser and the «Купил за…» dialog on phone [S57].

#### 6.8.9 Modal / confirm dialog
- 440 px max, radius `--r-xl`, padding 24.
- Title h2, one paragraph, actions right-aligned: «Отмена» (ghost) + the action (danger or primary).
- The destructive action's label is a verb + object («Удалить поиск»), never just «Да».
- Typed confirm for wipes: the input must equal «удалить».

#### 6.8.10 Toast
- Bottom-right (desktop, 24 px from edges) / bottom-center above the tab bar (phone). Width 360 / full minus 32.
- Surface `--ink` with `--on-ink` text (high contrast in both themes), radius `--r-lg`, `--shadow-toast`.
- Content: optional 16 px icon, message (14), **one** action button («Отменить», «Открыть», «Повторить») [S58].
- Duration: 4 s plain, 6 s with an action, 8 s for errors. Hover or focus pauses the timer. Max 3 stacked, newest on
  top; older ones collapse.
- Container `role="status" aria-live="polite"`. Errors use `role="alert"`.

#### 6.8.11 Stepper (onboarding)
- **Vertical (desktop):** items 40 px tall, 24 px circle (number / `check`) + label. Current: `--ink` circle + bold
  label. Done: `--green-solid` circle with a check, label `--text-2`, clickable. Future: `--border-strong` outline.
  Optional steps get a caption «необязательно».
- **Compact (phone):** «Шаг 3 из 8» (caption) + a 4 px bar, `--green-solid` fill, animated width 240 ms [S38].

#### 6.8.12 Tabs / segmented control
- 36 px, `--surface-2` track, radius `--r-md`.
- Selected segment is `--surface-1` + `--shadow-sm`, text `--text`; others `--text-2`.
- Counts in caption style.

#### 6.8.13 Stat tile
- Overline label, value (display 28–32 tnum), sub-line (body-sm `--text-2`), and an optional sparkline (40 px tall,
  1.5 px stroke `--text-3`, last point in the hue).
- No big coloured icons: a 20 px icon top-right in `--text-3` at most (quiet KPI row, Stripe-like [S59]).

#### 6.8.14 Meter (load / limits)
- 8 px bar, radius full. Fill is green < 60%, amber 60–85%, red > 85%.
- Label left, `64 / 150` on the right (tnum).
- A marker tick at the recommended 40% line for the request budget.

#### 6.8.15 Skeleton
- `--surface-2` blocks with radii matching the real content.
- Text bars 10–12 px tall at 60–90% width.
- Media blocks keep their exact aspect ratio (no layout shift).

#### 6.8.16 Tooltip / popover
- **Tooltip:** `--ink` bg, 12/500, radius `--r-sm`, 400 ms delay, max 240 px; desktop only.
- **Popover (the «?» help):** surface-1, `--shadow-lg`, radius `--r-lg`, 320 px, click-triggered (works on touch),
  closes on outside click or Esc.

#### 6.8.17 Price chart
Covered in §4.3.6. Colours come from tokens; axis labels caption `--text-3`; gridlines 1 px `--border` dashed.
No legend: label lines directly.

#### 6.8.18 Empty state block
Centered, max 420 px: 48 px icon in a 72 px `--surface-2` circle, h3 title, body `--text-2`, 1 primary + 1 secondary
button. Illustrations are icon-based only; no stock art.

### 6.9 Iconography
Use **Lucide** (ISC licence, 24 px grid, 2 px stroke, round caps) [S60]. Vendor the needed paths as an ES module
`icons.js` (`export const icons = { zap: '<path …/>', … }`), rendered as inline SVG with
`stroke="currentColor" fill="none" stroke-width="2"`. Sizes: 14 (meta rows), 16 (buttons, chips), 20 (nav, tiles),
24 (tab bar), 40 (category tiles, stroke 1.75).

| Purpose | Icons |
|---|---|
| Nav | `zap` Лента · `wallet` Мои сделки · `radar` Поиски · `activity` Состояние · `settings` Настройки |
| Categories | Handy `smartphone` · Notebooks `laptop` · Konsolen `gamepad-2` · PC-Zubehör `cpu` · Foto `camera` · Audio `headphones` · Tablets `tablet` · PCs `pc-case` · TV & Video `tv` · Videospiele `disc-3` · Haushaltsgeräte `washing-machine` · Fahrräder `bike` · Musikinstrumente `guitar` · Heimwerken `hammer` · Weitere Elektronik `plug` · Elektronik (all) `zap` |
| Decisions | buy `trending-up` · haggle `hand-coins` · bid `gavel` · watch `eye` · skip `eye-off` · personal `piggy-bank` · free `gift` |
| Pipeline | `star` · contacted `message-square` · bought `package-check` · sold `banknote` · hidden `eye-off` |
| Signals | `map-pin` · `clock` · `truck` · `package` (condition) · `scan-eye` (AI) · `shield-alert` (red flag) · `triangle-alert` (warning) · `store` (commercial seller) · `user` (private) |
| Actions | `copy` · `external-link` · `refresh-cw` · `play` · `pause` · `sliders-horizontal` · `arrow-up-down` · `ellipsis` · `x` · `check` · `chevron-left/right/up/down` · `plus` · `trash-2` · `pencil` · `scan-search` (check ad) |
| Integrations | `send` (Telegram) · `mail` · `bot` · `server` (local AI) · `key-round` (eBay keys) · `smartphone` / `qr-code` (phone access) · `database` · `hard-drive-download` (backup) · `scroll-text` (logs) |
| States | `hourglass` (learning) · `circle-check` · `circle-alert` · `info` · `circle-help` · `wifi-off` · `coffee` (quiet day) · `sun` / `moon` |

Brand: keep the existing green gradient logo (`static/logo.svg`) and favicon. The favicon shows a red dot badge
(swapped SVG) when Health is in error.

### 6.10 Implementation notes for a no-build Preact SPA
- **Vendoring:** vendor `preact`, `preact/hooks`, `@preact/signals`, `htm` as ESM files in `/static/vendor/`, mapped
  through an `<script type="importmap">` [S61]. **No CDN**: the app must work offline and keep traffic local.
- **One token file:** `tokens.css` (all §6 variables, both themes) plus `app.css` (components). Use no utility
  framework. Class names follow the component names used here (`.btn-primary`, `.chip`, `.deal-card`, `.decision-pill`,
  `.drawer`, `.sheet`, `.toast`, `.stepper`, `.meter`).
- **Theme bootstrapping:** an inline `<head>` script reads `localStorage['ebp-theme']` (existing key), else follows
  `prefers-color-scheme`. `<meta name="theme-color">` is updated on switch.
- **Router:** hash-based (`#/deal/123`), so the FastAPI catch-all only serves `index.html`, and Telegram links such as
  `http://host:8000/#/deal/123` work.
- **State:** signals for `deals`, `filters`, `health`, `settingsDraft`.
- **Live updates:** `EventSource('/api/events')` with fallback polling of `/api/health` every 30 s. Browser
  notifications (`Notification` API) are an opt-in toggle. They only work on `localhost` or HTTPS, not on plain LAN
  http, so hide the toggle there.
- **Formatting helpers** (one module, reuse Python's rules):
  - `fmtMoney(v)` → «1 250 €» (narrow NBSP U+202F thousands separator, NBSP before €, no decimals ≥ 100).
  - `fmtPct` → «38 %».
  - `fmtAgo` → «7 мин назад» / «вчера в 21:10».
  - `plural(n, 'находка','находки','находок')`.
  - Timezone Europe/Berlin.
- **Accessibility:**
  - Semantic landmarks.
  - Every icon-only button has `aria-label`.
  - Drawers and modals trap focus and restore it on close.
  - Colour is never the only signal: pills carry an icon and a verb.
  - Contrast ≥ 4.5:1 for text and 3:1 for UI borders.

---

## 7. Copy guide

### 7.1 Tone
- Friendly, direct, short. Talk like a smart friend who is good at deals: «Покупай», «Торгуйся», «Не бери».
- Address the user with **«ты»**. The app speaks in the first person: «Изучаю рынок», «Нашёл», «Уведомлю».
- Verbs on buttons (imperative or infinitive): «Проверить», «Скопировать», «Открыть ленту». Avoid «ОК» and «Да».
- One idea per sentence. Put numbers first when they matter: «+110 € прибыль», not «прибыль составит примерно 110 евро».
- No jargon without a hover explanation: ROI → «ROI 38 % — сколько заработаешь на каждый вложенный евро».
  VB → «Verhandlungsbasis — продавец готов торговаться».
- Errors: what happened, then what to do. Never blame the user; never show stack traces (hide them under «Подробности»).
- No exclamation marks except on real success («Готово!», «Пришло 🎉»). Emoji: at most one, only in success states.

### 7.2 Glossary (use consistently)

| Concept | Say | Don't say |
|---|---|---|
| listing | объявление | лот, айтем |
| good listing / evaluated result | находка / сделка | deal, лид |
| market price | рынок, «рынок ~420 €» | рыночная стоимость (except in the breakdown) |
| max_buy_price | «выгодно до 330 €» (auction: «ставь максимум 260 €») | макс. цена покупки |
| offer_price | «предложи 300 €» | оффер |
| expected_profit | «прибыль ≈ +110 €» / для себя «экономия 80 €» | маржа |
| verdict buy / maybe / skip | Покупай / Подумай / Не выгодно | buy/maybe/skip in UI |
| baseline run | «обучение», «изучаю рынок» | baseline |
| cooldown | «пауза после блокировки» | cooldown |
| searches | поиски | запросы, сёрчи |
| AI | нейросеть (short: ИИ in chips) | LLM, модель (except in settings) |

### 7.3 Key strings

**Buttons and actions**
- Onboarding: «Начать» · «Дальше» · «Назад» · «Пропустить» · «Запустить и открыть ленту»
- Checks: «Проверить» · «Проверить снова» · «Проверить на примере» · «Отправить тестовое сообщение»
- Feed: «Проверить сейчас» · «Пауза» · «Продолжить»
- Deal: «Написать продавцу» · «Скопировать» · «Скопировать и открыть объявление» · «Открыть объявление»
- Pipeline: «В избранное» · «Убрать из избранного» · «Написал» · «Купил» · «Продал» · «Скрыть» · «Вернуть»
- Searches: «Новый поиск» · «Сохранить» · «Отменить» · «Удалить поиск» · «Дублировать»
- Filters: «Сбросить фильтры» · «Показать все» · «Показать ещё»

**Hero and status lines**
- «Найдено {n} {выгодная находка|выгодные находки|выгодных находок} сегодня»
- «Лучшее сегодня: {title} — {price} → {profit}»
- «Работает · следующая проверка через {mm:ss}»
- «Идёт проверка… {i} из {n} поисков»
- «Пауза до {hh:mm}: Kleinanzeigen попросил отдохнуть (блокировка №{k}). Сам продолжу — ничего делать не нужно.»
- «Проверки на паузе. [Продолжить]»
- «Изучаю рынок: {i} из {n} категорий · собрано {m} цен»

**Toasts**
- «Сообщение скопировано — вставь его в чат продавцу»
- «Добавлено в избранное» · «Скрыто» (action «Отменить»)
- «Поиск сохранён · первая проверка — обучение»
- «Настройки сохранены»
- «Проверка запущена»
- «Новая находка: {title} · {profit}» (action «Открыть»)
- «Не получилось сохранить: {reason}» (action «Повторить»)

**Integration results**

| Integration | Success | Failure |
|---|---|---|
| AI | «Нейросеть работает · ответ за {s} с» | «LM Studio не отвечает. Открой LM Studio → Developer → Start Server» |
| Telegram | «Бот @{name} найден» · «Нашёл тебя: {first_name}» · «Тест отправлен — проверь Telegram» | «Ключ не подошёл — скопируй его у @BotFather ещё раз» · «Не дождался сообщения. Нажми Start в чате с ботом или введи chat_id вручную» |
| eBay | «Ключи работают · {limit} запросов в день» | «eBay не принял ключи — нужны Production-ключи, не Sandbox» |
| E-mail | «Письмо отправлено на {masked}» | «Gmail не пустил: нужен «пароль приложения», а не обычный пароль» |

**Confirmations**
- «Удалить поиск «{name}»? Найденные объявления останутся в ленте.» → «Удалить поиск»
- «Открыть панель для всей домашней сети? Любой в этом Wi-Fi со ссылкой сможет её открыть. Не делай так в общежитии или кафе.» → «Открыть для сети»
- «Сбросить историю цен? Оценки станут менее точными, пока программа снова не накопит цены (несколько дней).» → type «удалить»

**Warnings on cards and in the detail view**
- «⚠ Фото не проверены нейросетью — посмотри сам»
- «Мало данных о рынке — оценка примерная»
- «Может быть уже продано — объявлению {n} дн.»
- «Коммерческий продавец — торг маловероятен»

### 7.4 German seller-message templates (composer)
Rules [S19, S20, S21]:
- Greet, name the item briefly, give a concrete amount, offer pickup and cash, sign off.
- 2–3 sentences, no justification of the lower price, one offer per message.
- Pronoun: private sellers on Kleinanzeigen often use **«du»**, but «Sie» is never wrong with a stranger. Templates
  avoid a direct pronoun where possible and otherwise use «Sie» (a «du / Sie» toggle is a COULD).
- Placeholders: `{title}` (shortened product name: `ai.product` or the title cut to 40 chars), `{offer}`, `{price}`,
  `{day}` ∈ «heute» | «morgen» | «am Wochenende», `{time}` optional («gegen 18 Uhr»).

| Key | German (default text) | Russian (shown under it) |
|---|---|---|
| `offer` (default for haggle / VB) | «Hallo, ist {title} noch zu haben? Ich würde {offer} € bieten und könnte {day} abholen und bar bezahlen. Viele Grüße» | «Привет, {title} ещё продаётся? Предлагаю {offer} €, могу забрать {day} и заплатить наличными.» |
| `buy_now` (fixed price, great deal) | «Hallo, ich nehme {title} gern zum angegebenen Preis. Ich könnte {day}{time} vorbeikommen und bar bezahlen. Passt das? Viele Grüße» | «Привет, беру {title} по указанной цене. Могу заехать {day} и заплатить наличными. Удобно?» |
| `available` | «Hallo, ist {title} noch verfügbar? Ich könnte {day} abholen. Viele Grüße» | «Привет, {title} ещё доступен? Могу забрать {day}.» |
| `condition` (electronics) | «Hallo, ist {title} noch da? Funktioniert alles einwandfrei, gibt es Kratzer oder Defekte? Ich würde es bei der Abholung gern kurz testen. Viele Grüße» | «Всё работает? Есть царапины или дефекты? Хочу быстро проверить при встрече.» |
| `iphone_add` (appended for phones) | «Ist das Gerät aus iCloud abgemeldet und ohne Netlock? Wie hoch ist die Akkukapazität?» | «Устройство отвязано от iCloud и без блокировки оператора? Какая ёмкость аккумулятора?» |
| `gpu_add` | «Wurde die Karte zum Mining genutzt? Kann ich sie bei der Abholung kurz unter Last testen?» | «Карта была в майнинге? Можно проверить под нагрузкой при встрече?» |
| `laptop_add` | «Wie ist der Akkuzustand? Ist das Gerät zurückgesetzt und ohne BIOS-Passwort?» | «Какое состояние батареи? Ноутбук сброшен, без пароля BIOS?» |
| `shipping` | «Hallo, wäre auch Versand über „Sicher bezahlen“ möglich? Dann würde ich {offer} € inklusive Versand vorschlagen. Viele Grüße» | «Можно отправить через «Sicher bezahlen»? Тогда предлагаю {offer} € с доставкой.» |
| `counter` (after a reply) | «Danke für die schnelle Antwort! Wären {offer} € in Ordnung? Dann komme ich {day} vorbei.» | «Спасибо! {offer} € подойдёт? Тогда приеду {day}.» |
| `bundle` | «Hallo, ich hätte Interesse an mehreren Artikeln ({title} und …). Würden Sie mir alles zusammen für {offer} € geben? Abholung {day}, Barzahlung.» | «Хочу взять несколько вещей. Отдадите всё вместе за {offer} €?» |
| `decline` | «Danke, dann passt es leider nicht für mich. Viel Erfolg beim Verkauf!» | «Спасибо, тогда не подходит. Удачной продажи!» |

Composer logic:
- Default template: `offer` when `action=haggle` or the listing is `negotiable`; `buy_now` when `action=buy` and the
  price is at least 20% below `max_buy_price`; otherwise `available`.
- Category add-ons are appended automatically from `ai.product` / category keywords (iPhone|Galaxy|Pixel → phone;
  RTX|GTX|Radeon → GPU; Notebook|Laptop|MacBook|ThinkPad → laptop). Each can be removed with a chip `x`.
- `{offer}` rounds to a "human" number: to 5 € under 100 €, to 10 € above 100 €. «Ich würde 295 € bieten» reads better
  than «293 €».

### 7.5 «Проверь при встрече» checklists (by product type)

| Type | Checklist items |
|---|---|
| Телефон | «iCloud/Google-аккаунт отвязан (Настройки → имя)» · «IMEI в настройках = IMEI на коробке» · «Ёмкость аккумулятора ≥ 85 %» · «Face ID / отпечаток работает» · «Камеры, динамик, микрофон» · «Нет «неоригинальный дисплей»» [S29] |
| Видеокарта | «Запусти тест под нагрузкой 5–10 мин (FurMark/игра)» · «Вентиляторы крутятся тихо, без треска» · «Нет следов вскрытия и перегрева» · «Модель и память совпадают (GPU-Z)» [S30] |
| Ноутбук | «Аккумулятор: износ и циклы» · «Нет пароля BIOS/аккаунта, Windows сброшена» · «Клавиатура, тачпад, порты, петли» · «Экран без битых пикселей» · «Зарядка оригинальная» |
| Консоль | «Включается, читает диск/скачивает» · «Аккаунт отвязан» · «Нет бана (онлайн работает)» · «Геймпады держат заряд» |
| Общее | «Плачу наличными при встрече или через «Sicher bezahlen»» · «Не перевожу деньги заранее» · «Встреча в людном месте» [S25] |

---

## 8. Backend gaps the UI depends on (for the implementation team)

The data models already cover evaluation, listings and runs. The redesign needs these additions. Names are
suggestions; keep them RESTful JSON under `/api`.

| Area | Need | Why |
|---|---|---|
| Pipeline | `DealStatus` += `"sold"` (and optionally `"lost"`); new fields in `deal_state`: `bought_price`, `bought_at`, `sold_price`, `sold_at`, `extra_costs`, `hidden_reason`, `contacted_at` | §4.4 real profit, nudges, feedback |
| Pipeline stats | `GET /api/pipeline/summary` → earned by month, invested, expected, accuracy | §4.4 tiles |
| Feed | `/api/deals` filters: `action`, `max_km`, `shipping`, `no_flags`, `unseen`, `since`; sort `fresh`, `distance`, `ending` | §4.2.2 chips |
| Feed hero | `GET /api/summary/today` → count, potential profit, best deal, learning progress | §4.2.1 |
| Live | `GET /api/events` (SSE): `deal.new`, `run.started`, `run.progress`, `run.finished`, `health.changed`, `setting.saved` | §2.8, §4.2.6 |
| Market chart | `GET /api/deals/{id}/market` → price points for the product key + median/p25/p75 + max_buy_price | §4.3.6 (the `price_points` table already exists) |
| Setup | `GET /api/locations?q=`; `GET /api/categories?location&radius&live`; `GET /api/setup/estimate` (interval + load %); `PUT /api/setup/draft`; `POST /api/setup` (JSON version of the existing form POST) | §4.1 |
| AI | `POST /api/ai/test` (bundled sample ad → structured result + seconds) | §4.1.6 |
| Telegram | `POST /api/telegram/validate` (getMe), `POST /api/telegram/link` (returns start code + deep link), `GET /api/telegram/detect?code=` (getUpdates match), `POST /api/telegram/reset-webhook` | §4.1.7 |
| eBay / e-mail | `POST /api/ebay/validate`, `POST /api/email/validate`; `POST /api/notify/test?channel=` | §4.1.8 |
| Settings | `GET /api/settings` (masked secrets) / `PATCH /api/settings/{section}` with per-field validation errors `{field: message}`; secrets written to `.env` server-side | §4.7 |
| Notify preview | `GET /api/notify/preview?min_score&verdicts` → how many alerts in the last 7 days | §4.7.4 |
| Monitor control | `POST /api/pause`, `POST /api/resume` (persisted), run progress in `/api/health` | §4.6 |
| Logs | `GET /api/logs?level&q&limit`, `GET /api/logs/stream`, `GET /api/logs/download` | §4.6 |
| Phone access | `GET/PUT /api/access` (mode, host, token rotate, QR payload) | §4.7.6 |
| Data | `GET /api/backup` (zip), `POST /api/restore`, `GET /api/export/deals.csv`, `POST /api/data/reset-history` | §4.7.7 |
| Check one ad | `POST /api/check` `{url, purpose, target}` → job id + progress events → DealView | §4.8 |
| Demo | `POST /api/demo/load`, `POST /api/demo/clear` | §4.1.1 |
| Seen state | optional `POST /api/deals/{id}/seen` (else localStorage) | §4.2.3 |

---

## 9. Prioritized MVP scope

### MUST (v1: "no CLI ever" is true)
1. App shell: sidebar / rail / bottom tabs, hash router, tokens (light + dark), Inter vendored, Lucide subset, toasts,
   drawer/sheet, confirm dialog, skeletons.
2. **Onboarding**, all 9 steps. Includes city autocomplete, radius slider, category cards with recommended preselected,
   money presets with a live example, wishlist rows, AI auto-detect + «Проверить на примере», **Telegram guided flow
   with auto chat-id detection** and test, eBay keys + validate, Done with the learning-pass explanation.
   Draft persistence.
3. **Лента:** hero summary + status strip with countdown and «Проверить сейчас», learning state, setup-checklist card,
   filter chips (action / purpose / distance / no flags) + sort, deal card (grid) and deal row (phone), star / hide
   with undo, «Написать» (copy German message + open ad), infinite scroll, live new-deal toast + «↑ N новых» (SSE, or
   30 s polling as the MVP fallback).
4. **Сделка:** gallery with lightbox, decision block for buy / haggle / bid / maybe / personal / unchecked, red flags,
   profit breakdown, comparables list, AI findings, safety tips, message composer (templates offer / buy_now /
   available / condition + category add-ons, Russian translation), status actions incl. «Купил за…» dialog, notes.
5. **Мои сделки:** 4 columns / segmented list with the `sold` status, buy / sell price dialogs, real profit per item,
   summary tiles (earned, invested, expected).
6. **Поиски:** cards with toggle, stats and state badge; add chooser (category / keywords / URL / wishlist / eBay);
   edit side panel; delete with confirm; load meter.
7. **Состояние:** top banner, the 6 status tiles with actions (run now, pause / resume, test AI, test notifications),
   runs table.
8. **Настройки:** Поиск и регион, Деньги, Нейросеть, Уведомления, eBay, Доступ с телефона (modes + token link),
   Данные (backup download, reset with typed confirm). Sticky save bar, per-field validation.
9. All states from §5: empty, loading, error, offline.

### SHOULD (v1.1)
- Market price-history chart (§4.3.6), with the new endpoint.
- Haggle offer slider with live profit.
- Notification-threshold live preview («пришло бы 14 за 7 дней»).
- Logs viewer with level filter and live tail.
- Hide-reason feedback chips + a weekly «шумные поиски» hint on the Searches page.
- Kanban drag-and-drop; swipe gestures on phone rows.
- Keyboard shortcuts (`j/k/s/h/c/o`, `?` sheet).
- QR code for phone access.
- «Проверить объявление» modal (replaces `check <url>`).
- Predicted-vs-actual accuracy tile.
- Server-side «seen» state.
- Browser notifications on localhost.

### COULD (later)
- «Напомнить за 10 мин до конца аукциона» Telegram ping.
- CSV export of the pipeline; restore from backup.
- «du / Sie» toggle and user-editable templates.
- Translate the German description to Russian using the local model.
- «Переоценить» action.
- PWA manifest + «Добавить на главный экран» (only meaningful on localhost/HTTPS).
- Update checker in «О программе».
- Stats page (profit per category, best hours of day for deals).

### WON'T (explicitly out of scope)
- Messaging sellers automatically or logging into Kleinanzeigen or eBay accounts (ToS / ban risk; copy-and-open only).
- Auto-bidding or sniping on eBay.
- Cloud sync or accounts; anything loaded from third-party CDNs.

---

## 10. Sources

Flippers' needs and deal-alert tools
- S1 — How to message a seller on FB Marketplace (speed, first 15 min, 10–30 messages/hour): https://carsnipe.com/blog/how-to-message-seller-facebook-marketplace-cars
- S2 — Negotiating on Marketplace without angering the seller: https://maggiemcgaugh.com/blog/negotiating
- S3 — Facebook Marketplace alerts reseller playbook (15 min – 4 h native delay; missing negative keywords / margin thresholds; community reports from r/FacebookMarketplace and r/Flipping): https://www.superflip.ai/facebook-marketplace-alerts
- S4 — Kleinanzeigen iOS app: no notifications although allowed (ComputerBase): https://www.computerbase.de/forum/threads/kleinanzeigen-ios-app-keine-benachrichtigungen-obwohl-erlaubt.2230461/
- S5 — Kleinanzeigen saved searches stopped notifying (V4-Forum) and "monitors notify hours later": https://v4-forum.de/threads/ebay-kleinanzeigen-keine-benachrichtigung-mehr-zu-gespeicherten-suchen.20660/ , https://kleinanzeigen-benachrichtigung.de/
- S6 — Swoopa reviews (10–30 min delays at $145/mo, loose filters, cost, trial): https://justuseapp.com/en/app/6475300269/swoopa/reviews
- S7 — Swoopa pricing analysis ($47–$352/mo): https://carsnipe.com/blog/swoopa-pricing
- S8 — Swoopa alternatives / verified-profit scanners (latency ~5 min on paid tiers): https://www.superflip.ai/swoopa-alternatives
- S9 — Botifex: best marketplace alert tools 2026 (median 4 min 12 s; silence false positives, review weekly): https://botifex.com/blog/best-marketplace-alert-tools-2026
- S10 — DealScout (exact-word matching; avoid duplicate scouts): https://dealscout.app/ , https://apps.apple.com/us/app/dealscout/id6743296610
- S11 — Flipify (negative keywords, AI dedupe/junk removal, deal ranking vs eBay sold): https://www.flipifyapp.com/ , https://www.capterra.com/p/10050498/Flipify/
- S12 — Flipify review (pricing, verdict): https://outpostalerts.com/blog/flipify-review
- S13 — Reports of inflated profit ignoring eBay fees; sold-comp verification (competitor source, biased): https://www.superflip.ai/swoopa-alternatives
- S14 — FB Marketplace scanner tools comparison 2026: https://www.superflip.ai/resources/facebook-marketplace-scanner-tools
- S15 — Underpriced (0–100 deal score, AI confidence, red flags, flip tracker): https://www.underpriced.app/
- S16 — Spottable (0–100 AI score, fraud flags, hourly vs minute alerts): https://spottableapp.com/ , https://apps.apple.com/us/app/spottable-marketplace-alerts/id6758812746
- S17 — Terapeak / sell-through (50%+ healthy, <20% slow; median, p25–p75): https://closo.co/blogs/community/the-ebay-crystal-ball-how-to-master-terapeak-ebay-research-in-2026
- S18 — eBay Product research help: https://www.ebay.com/help/selling/selling-tools/product-research?id=4853
- S19 — Kleinanzeigen deals & haggling guide (concrete amount + pickup + cash): https://sniping-tool.de/guide/kleinanzeigen-deals-finden
- S20 — Kleinanzeigen «Kleiner Knigge» (VB etiquette, one polite offer): https://themen.kleinanzeigen.de/kleiner-knigge/
- S21 — Kleinanzeigen sample messages to sellers: https://themen.kleinanzeigen.de/magazin/tipps/kaufer-kontaktieren/
- S22 — Kleinanzeigen «Sicher bezahlen» / buyer protection: https://hilfe.kleinanzeigen.de/hc/de/articles/17211553583388-Was-ist-Sicher-bezahlen-wie-funktioniert-der-K%C3%A4uferschutz
- S23 — Sicher bezahlen fees (0.50 € + 4.5 %): https://www.monsterdealz.de/magazin/kleinanzeigen-sicher-bezahlen
- S24 — Gixen FAQ (max bid placed seconds before end; proxy bidding): https://www.gixen.com/main/faq.php
- S25 — Verbraucherzentrale: Betrug mit Kleinanzeigen: https://www.verbraucherzentrale.de/wissen/digitale-welt/onlinehandel/betrug-mit-kleinanzeigen-diese-maschen-sollten-sie-kennen-110389
- S26 — Kleinanzeigen Help: sicher handeln, Betrug erkennen: https://hilfe.kleinanzeigen.de/hc/de/articles/22774175164572-Sicher-handeln-auf-Kleinanzeigen-so-erkennst-du-Betrug
- S27 — TECHBOOK: Fake-Maschen auf Kleinanzeigen (off-platform chat, too-cheap prices, new profiles, reverse image search): https://www.techbook.de/shop-pay/shops-marktplaetze/fake-abzocken-ebay-kleinanzeigen
- S28 — t3n: Kleinanzeigen-Betrug erkennen: https://t3n.de/news/kleinanzeigen-betrug-fake-zahlung-mitleidsstory-phishing-erkennen-1724757/
- S29 — Sourcing & flipping used electronics (iCloud / IMEI checks): https://closo.co/blogs/blog/the-honest-resellers-blueprint-to-sourcing-and-flipping-used-electronics-in-2026
- S30 — Buying used GPUs safely (mining wear, stress test): https://www.rankedgpu.com/guides/used-gpus-smart-buying
- S31 — Flippd features (true profit, inventory aging, death pile helper): https://getflippd.com/features/
- S32 — Reseller Profit Tracker / Flippd (sold items, dead stock): https://apps.apple.com/us/app/reseller-profit-tracker-flip/id6760418106
- S33 — How to read a Keepa graph (range selector, hover, toggles): https://www.sellerassistant.app/blog/how-to-read-a-keepa-graph
- S34 — CamelCamelCamel vs Keepa: https://goaura.com/blog/camelcamelcamel-vs-keepa
- S35 — camelcamelcamel.com (price drop alerts, target price): https://camelcamelcamel.com/
- S36 — kleinanzeigen-alert (Telegram bot; Docker/env setup): https://github.com/DanielStefanK/kleinanzeigen-alert
- S37 — KleinanzeigenTelegramBot / ebay-kleinanzeigen bots: https://github.com/JoeKL/KleinanzeigenTelegramBot , https://github.com/okainov/ebay-kleinanzeigen

Onboarding, UX patterns, design references
- S38 — Stepper UI examples (3–6 steps, back navigation, non-linear): https://www.eleken.co/blog-posts/stepper-ui-examples
- S39 — ProductLed SaaS onboarding best practices (checklists): https://productled.com/blog/5-best-practices-for-better-saas-user-onboarding
- S40 — Candu onboarding examples (3–5 checklist items tied to goals; empty states): https://www.candu.ai/blog/best-saas-onboarding-examples-checklist-practices-for-2025
- S41 — Linear: How we redesigned the Linear UI (inverted-L chrome, dimmer sidebar, LCH, Inter Display): https://linear.app/now/how-we-redesigned-the-linear-ui ; tokens: https://github.com/VoltAgent/awesome-design-md/blob/main/design-md/linear.app/DESIGN.md
- S42 — Material 3 navigation bar (3–5 destinations, <600dp) and breakpoints (600/840): https://m3.material.io/components/navigation-bar/guidelines , https://m3.material.io/foundations/layout/breakpoints/medium
- S43 — Raycast design tokens (surface ladder, no shadows, keycaps, 8 px radii): https://github.com/VoltAgent/awesome-design-md/blob/main/design-md/raycast/DESIGN.md
- S44 — Telegram bot features: deep linking `?start=` parameter: https://core.telegram.org/bots/features
- S45 — Getting a chat_id via getUpdates after /start: https://gist.github.com/nafiesl/4ad622f344cd1dc3bb1ecbe468ff9f8a
- S46 — Side sheets vs modals, master-detail triage pattern: https://www.jobpreparena.com/blog/designing-contextual-side-sheets-drawers-vs-modals-in-highly-dense-workspace-applications , https://blogs.windows.com/windowsdeveloper/2017/05/01/master-master-detail-pattern/
- S47 — Skeleton screens vs spinners (timing thresholds): https://blog.logrocket.com/ux-design/skeleton-loading-screen-design/ , https://www.onething.design/post/skeleton-screens-vs-loading-spinners
- S48 — Wise design tokens (lime CTA with ink text, 24 px radii, semantic colours): https://github.com/VoltAgent/awesome-design-md/blob/main/design-md/wise/DESIGN.md
- S49 — Revolut design tokens and fintech patterns (48 px touch targets, pill chips, hero balance): https://github.com/VoltAgent/awesome-design-md/blob/main/design-md/revolut/DESIGN.md , https://www.eleken.co/blog-posts/trusted-fintech-ui-examples
- S50 — Airbnb listing card anatomy (square photo, heart top-right, badge top-left, photo carries the card): https://github.com/VoltAgent/awesome-design-md/blob/main/design-md/airbnb/DESIGN.md
- S51 — Radix Colors: understanding the 12-step scale: https://www.radix-ui.com/colors/docs/palette-composition/understanding-the-scale
- S52 — Dark mode best practices (no pure black, lighter elevation, desaturated accents): https://atmos.style/blog/dark-mode-ui-best-practices , https://blog.logrocket.com/ux-design/dark-mode-ui-design-best-practices-and-examples/
- S53 — MDN font-variant-numeric: https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/font-variant-numeric
- S54 — Stripe design tokens (tnum on money, pill buttons, 8 px spacing): https://github.com/VoltAgent/awesome-design-md/blob/main/design-md/stripe/DESIGN.md
- S55 — Vercel Geist tokens (gray scale, 4/6 px radii, Geist Mono): https://vercel.com/geist/colors , https://seedflip.co/blog/vercel-design-system
- S56 — Linear design-system reference (flat surfaces, shadows only on floating layers, ~200 ms motion): https://github.com/marcus/marcus-skills/blob/main/skills/linear-design-patterns/references/linear-design-system.md
- S57 — Bottom sheets UX guidelines (NN/g; LogRocket): https://www.nngroup.com/articles/bottom-sheet/ , https://blog.logrocket.com/ux-design/bottom-sheets-optimized-ux/
- S58 — Accessible toasts (duration, single action, aria-live): https://accessibility.build/guides/accessible-notifications
- S59 — Stripe dashboard breakdown (calm KPI row, detail on demand): https://www.925studios.co/blog/stripe-dashboard-design-breakdown
- S60 — Lucide icons: https://lucide.dev/icons/ , https://github.com/lucide-icons/lucide
- S61 — Preact no-build workflows (htm, import maps, signals): https://preactjs.com/guide/v10/no-build-workflows/
