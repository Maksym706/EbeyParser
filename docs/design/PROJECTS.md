# «Сборки» — build projects: plan a PC from used parts, then catch every part cheaply

**For:** the frontend agent building the SPA screens (`ebeyparser/web/app/**`). The backend is done:
`/api/v1/projects*` (see §9), the `project_updated` SSE event, Telegram / e-mail alerts.
**Style:** everything in [DESIGN_BRIEF.md](DESIGN_BRIEF.md) applies — tokens (§6.1), components (§6.8), copy rules
(§7), states (§5), «ты», no CLI / config words, decision first and evidence second.

---

## 0. The feature in one paragraph

The user in his own words: *«Мне нужно купить комплектующие для сервера под LLM по лучшей цене — приложение должно
помогать и с этим».* He writes a goal and a budget («Сервер для локальных LLM, чтобы тянул 70B в Q4, бюджет 1500 €»)
or taps a template. The app proposes a **parts plan**: slots (видеокарты, процессор, плата, память, блок питания, SSD,
корпус, охлаждение…) with **alternatives** (2× RTX 3090 | 2× Tesla P40 | 2× MI50 32 ГБ …). It **checks compatibility**
and shows the maths in plain Russian (video memory for the model, PSU watts, PCIe slots, RAM type, a monitor output).
One button, «Начать отслеживание», creates a personal search for every part. From then on the build shows the
**best current offer per slot**, the **total against the budget and against buying new**, and it **alerts** when an
offer is below its target or when the whole build fits the budget. «Купил» fills the slot, recalculates the budget
and pauses that part's searches.

Prices are always **market data** (the app's own price history and the ads of the project's searches). Where there
is none yet, the plan shows a rough **«ориентир»** from the built-in knowledge base, clearly marked
(`price.rough = true`). The local AI (when on) only helps *understand the goal and choose among known parts*. It
never names a price.

---

## 1. Where it lives

| Surface | Decision |
|---|---|
| Desktop / tablet sidebar | New nav item **«Сборки»**, Lucide `boxes`, between «Мои сделки» and «Поиски». Badge = number of tracking projects whose `headline_ru` is not empty (an offer below target is waiting). |
| Phone (bottom tab bar, max 5) | No 6th tab. «Мои сделки» gets a top **segmented control «Сделки · Сборки»**; `/projects` opens it on «Сборки». |
| Feed (Лента) | When a tracking project has an offer below target, show one compact card above the feed: `boxes` icon, «Сборка «LLM-сервер»: MI50 за 175 € — ниже цели», button «Открыть сборку». |
| Deal detail | If the deal belongs to a project search (`card.search_name` is in some project's `searches_list`), add a chip «Для сборки «LLM-сервер»» under the title, linking to the project. |
| Поиски | Project searches are ordinary personal searches, named `«<project name> · <part>»`. Group them under a section «Сборки» (search `name` in `searches_list` of `GET /projects/:id`); their overflow menu gets «Открыть сборку». |

**Routes** (history API, like the rest of the SPA; the server already answers any page path with the SPA):

| Route | Screen |
|---|---|
| `/projects` | list (§3) |
| `/projects/new` | creation (§4) |
| `/projects/new?template=llm_48` | creation with a template preselected |
| `/projects/:id` | project: plan view while `status = draft`, tracking view otherwise (§5, §6) |
| `/projects/:id/slot/:slot` | phone only: one slot full-screen (offers, alternatives, trend) |

Links in Telegram / e-mail alerts point to `/projects/:id`.

---

## 2. Vocabulary (use consistently)

| Concept | Say | Don't say |
|---|---|---|
| project | **сборка** | проект, билд |
| slot | **часть** («Видеокарты», «Блок питания») | слот |
| option | **вариант** | альтернатива (ok in help text), опция |
| target price | **цель** («цель 190 €») | таргет |
| max price (target × 1.3) | «проверяю до 250 €» | максимальная цена поиска |
| offer | **предложение** / объявление | оффер |
| rough price | «ориентир» + the `price.label_ru` text | «примерная цена» without a source |
| market price | «рынок ~220 €» | |
| compatibility check | **проверка** | валидация |
| tracking | «отслеживаю», «слежу» | мониторинг |

Status labels come from the API (`status_label`): Черновик / Отслеживаю / На паузе / Собрано. Slot labels: Нужно
купить / Ищу / Куплено / Уже есть / Не нужно.

---

## 3. List — `/projects`

```
Desktop                                                        Phone (Мои сделки → [Сделки|Сборки])
┌──────────────────────────────────────────────────────────┐   ┌──────────────────────────┐
│ Сборки                                   [+ Новая сборка] │   │ [ Сделки | Сборки ]       │
│ Собираю компьютеры из б/у деталей по лучшей цене          │   │ ┌──────────────────────┐ │
│ ┌──────────────────────────┐ ┌──────────────────────────┐ │   │ │ ▣ LLM-сервер: 70B Q4 │ │
│ │ ▣ LLM-сервер: 70B Q4_K_M │ │ ▣ NAS на 8 ТБ            │ │   │ │ Отслеживаю · 2 из 8  │ │
│ │ Отслеживаю     ◔ 2 из 8  │ │ Черновик       ○ 0 из 6  │ │   │ │ ~1 005 € из 1 500 €  │ │
│ │ ~1 005 € из 1 500 €      │ │ ~560 € из 600 €          │ │   │ │ ● Ниже цели: MI50 …  │ │
│ │ ● Ниже цели: MI50 за 175€│ │                          │ │   │ └──────────────────────┘ │
│ │ [Посмотреть]   3 увед.   │ │ [Начать отслеживание]    │ │   │  [+ Новая сборка]        │
│ └──────────────────────────┘ └──────────────────────────┘ │   └──────────────────────────┘
└──────────────────────────────────────────────────────────┘
```

- `GET /api/v1/projects` → `items[]` (ProjectCard, §9.3).
- **Card:** 40 px icon tile (`icon`: `cpu` / `server` / `hard-drive` / `gamepad-2` / `list-checks`), `name`
  (16/600), `template_label` (13, `--text-2`), a status chip (`status_label`; tracking = green dot, paused = grey,
  draft = neutral, done = green check), a **progress ring** (`progress`, 0..1) with `progress_label`, and
  `total_label` (tnum). It turns `--green-text` when `fits_budget === true`, and gets an amber «над бюджетом» chip
  when `over_budget > 0`. Below it sits `headline_ru` (green dot, 1 line) when not empty. The primary button follows
  `next_step.key`: `track` → «Начать отслеживание», `buy` → «Посмотреть», `resume` → «Продолжить», `wait` →
  «Открыть», `done` → «Открыть». Meta: «N уведомлений» (`alerts`), «обновлено {updated_at_label}».
- Sort: tracking first, then drafts, then done; within a group by `updated_at` (the API already returns newest first).

---

## 4. Creation — `/projects/new`

One screen, two steps. The plan preview is **not saved** until the user presses save (`POST /projects/plan` is a
dry run).

### 4.1 Step 1 «Что собираем?»

```
┌───────────────────────────────────────────────────────────────────────────┐
│ Что собираем?                                                              │
│ ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐ │
│ │ ▣ cpu      │ │ ▣ server   │ │ ▣ hard-dr. │ │ ▣ gamepad  │ │ ▣ list     │ │
│ │ LLM-сервер │ │ LLM-сервер │ │ Домашний   │ │ Игровой ПК │ │ Свой       │ │
│ │ 24 ГБ VRAM │ │ 48 ГБ VRAM │ │ NAS        │ │ 1080p      │ │ список     │ │
│ │ до ~32B    │ │ 70B в Q4   │ │ файлы, фото│ │ Full HD    │ │ что угодно │ │
│ └────────────┘ └────────────┘ └────────────┘ └────────────┘ └────────────┘ │
│ Или опиши своими словами                                                   │
│ ┌───────────────────────────────────────────────────────────────────────┐ │
│ │ Сервер для локальных LLM, чтобы тянул 70B в Q4, бюджет 1500 €          │ │
│ └───────────────────────────────────────────────────────────────────────┘ │
│ Бюджет [ 1500 ] €         ◉ Нейросеть поможет понять цель своими словами   │
│                                                   [ Составить план → ]     │
└───────────────────────────────────────────────────────────────────────────┘
```

- **Template cards** come from `GET /api/v1/projects/templates` → `items[]` {`key`, `title`, `subtitle`, `icon`,
  `default_budget`, `example_goal`}. Tapping a card selects it (green border, like the onboarding category cards) and
  fills the budget with `default_budget` if the field is empty. The goal textarea shows `example_goal` as its
  placeholder. The choice is optional: a goal alone is enough, the server picks the template.
- **Goal textarea:** 3 rows, max 2000 chars. Helper: «Например: „сервер для нейросетей, 70B в Q4, бюджет 1500 €“ или
  „ThinkPad T480 до 200 €, док-станция, монитор 27 дюймов — бюджет 400 €“».
- **Budget:** number input with «€», optional (the goal may already name it). 1–100 000.
- **«Свой список»:** the textarea switches to a row editor `[что ищешь — как пишут в объявлениях] [до … €] [× шт] [×]`
  plus «+ Добавить». Send the rows as `items[]` ({`label`, `query`, `target_price`, `qty`}). Helper under the
  first row: «Пиши так, как продавцы называют вещь: „ThinkPad T480“, „Monitor 27 Zoll“».
- **AI chip** from `templates.ai`: available → blue chip `bot` «Нейросеть поможет понять цель своими словами»;
  unavailable → neutral chip with `ai.label_ru` and a link «Включить» → `/settings/ai`. When available, a small
  toggle «С нейросетью» (default on) maps to `use_ai`.
- **«Составить план»** → `POST /api/v1/projects/plan` with {`goal`, `budget`, `template`, `use_ai`, `items`}.
  - Loading: without AI < 300 ms (button spinner). With AI it can take up to 2 minutes, so show a skeleton of the plan
    with the label «Нейросеть читает цель и выбирает детали…», and after 20 s add «Можно подождать или продолжить без
    нейросети» + a ghost button that repeats the call with `use_ai: false`.
  - 422 → inline error on the field from `error.fields` (`goal`, `budget`, `template`).

### 4.2 Step 2: the plan preview

The same layout as the plan view (§5), with a sticky footer:
**«Сохранить и начать отслеживание»** (primary) · «Сохранить черновик» (ghost) · «Назад» (icon).

- Save = `POST /api/v1/projects` with `{"plan": <preview.plan>, "choices": {<slot>: <option>, …}, "name": …}`.
  The `plan` object is returned by the preview verbatim (field `plan`). Send it back unchanged. Put the user's picks
  in the preview into `choices`, and the server re-fits the dependent parts.
- «Сохранить и начать отслеживание» = the save above, then `POST /projects/:id/track` (first with `dry_run: true`
  if the load confirmation is shown, §6.1).

---

## 5. Plan view (draft project, and the preview)

```
Desktop (content column, max 1100)
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ ‹ Сборки   LLM-сервер: 70B Q4_K_M  ✎                     Черновик   [Начать отслеживание]│
│ LLM-сервер для 70B Q4_K_M · бюджет 1 500 €                                              │
│ ┌───────────────── summary (hero card) ────────────────────────────────────────────┐  │
│ │ 2× AMD Instinct MI50 32 ГБ + остальное: ~1 065 € по рынку при бюджете 1 500 €.     │  │
│ │ 64 ГБ ≥ нужно ≈44 ГБ — запас 20 ГБ; ≈ 8,4–13 ток/с.                               │  │
│ │ [По рынку ~1 065 €] [Цель 925 €] [Бюджет 1 500 €] [Новым ~885 € без видеокарт]    │  │
│ │ Понял так: бюджет 1 500 €, модель 70B, квантизация Q4_K_M            ◌ ориентир  │  │
│ └──────────────────────────────────────────────────────────────────────────────────┘  │
│ ┌ Проверки: всё совместимо ────────────────────────────────── [Показать расчёты ▾] ┐  │
│ │ ✓ Видеопамять 64 ГБ ≥ ≈44 ГБ  ✓ Блок питания 1 000 Вт  ✓ Слоты PCIe  ✓ Память DDR4 │  │
│ │ ✓ Видеовыход (Ryzen 5 5600G)  ✓ Охлаждение  ℹ Разъёмы питания  ✓ Бюджет          │  │
│ └──────────────────────────────────────────────────────────────────────────────────┘  │
│ Видеокарты  ─ Главное для нейросети — сколько видеопамяти и насколько она быстрая      │
│ ┌──────────────┬────────┬───────────┬────────────┬──────────┬────────────┬─────────┐  │
│ │ Вариант      │ Память │ Скорость  │ Видеокарты │ Вся сб.  │ Ценность   │         │  │
│ │◉ 2× MI50 32  │ 64 ГБ ✓│ 8,4–13 т/с│ ~440 €     │ ~1 065 € │ 89 Отлично │ ⚠ ROCm  │  │
│ │○ 2× P40 24   │ 48 ГБ ✓│ 3,7–5,3   │ ~500 €     │ ~1 095 € │ 79         │ ⚠ Pascal│  │
│ │○ 2× RTX 3090 │ 48 ГБ ✓│ 12–17     │ ~2 100 €   │ ~2 775 € │ 48  над бюджетом     │  │
│ └──────────────┴────────┴───────────┴────────────┴──────────┴────────────┴─────────┘  │
│ Процессор          ◉ Ryzen 5 5600G  ○ Ryzen 5 5600  ○ Ryzen 7 5700X  ○ Xeon E5-2680 v4│
│ …one card per part…                                                                   │
│ Не нужно для этого варианта: Райзеры PCIe (карты стоят прямо в корпусе) · Кулер …     │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

### 5.1 Header + summary hero
- Title `name` (inline rename → `PATCH {name}`), sub-line `template_label` · «бюджет {budget}» (inline edit →
  `PATCH {budget}`).
- Hero card (16 px radius): `summary_ru` (body-lg), then **4 stat chips** from `totals`: «По рынку ~{typical_total}»,
  «Цель {target_total}» (tooltip: «если брать каждую часть на ~10 % дешевле рынка»), «Бюджет {budget}», and
  `new_label_ru` (neutral). Below them: `totals.stretch_label_ru` (amber when `ratio < 0.9`, red when `< 0.65`).
- `notes[]` render as a quiet line; the first one starts with «Понял так:». Render it as chips after the colon, so
  the user sees what the app understood.
- When `prices_rough`: a small `◌ ориентир` badge with a popover showing `prices_note_ru`.
- `ai`: `ai.used && ai.ok` → a blue card `bot` with `ai.summary_ru` + `ai.message_ru` (caption). `ai.ok === false`
  → an amber caption with `ai.message_ru` («Нейросеть не ответила — план составлен по правилам»). Not used → nothing
  (or the caption, if `message_ru` says the AI is off).

### 5.2 Checks panel («Проверки»)
- Header: `checks_summary.text_ru`, tinted by `checks_summary.status` (ok green / warn amber / fail red).
- Collapsed: one row of chips (✓ / ! / ✕ / ℹ + `title` + short `summary`).
- Expanded («Показать расчёты»): one row per check: icon, **`title`**, `summary`, then `lines[]` as the maths in a
  mono-ish tnum block. These lines ARE the plain-Russian maths («Веса: 70 млрд параметров × 4,85 бита (Q4_K_M) ÷ 8
  ≈ 39,5 ГБ»…). Keep them visible; they are the point of this screen.
- **Fix button:** when `fix` is not null → a small button `fix.label_ru` («Взять блок питания 1300 Вт (80+ Gold)») →
  `PATCH /projects/:id {"choices": {fix.slot: fix.option}}` (in the preview: change the choice locally and re-send
  the preview with `choices`, or save first). The response is the updated project. Animate the changed slot.
- Check keys the backend sends: `vram`, `speed`, `psu`, `pcie`, `spacing`, `socket`, `ram`, `display`, `cooling`,
  `power`, `capacity`, `sata`, `electricity`, `budget`. Icons: `memory-stick` (vram), `gauge` (speed), `plug-zap`
  (psu, power), `cpu` (socket), `layout-grid` (pcie, spacing), `monitor` (display), `fan` (cooling),
  `hard-drive` (capacity, sata), `zap` (electricity), `wallet` (budget).

### 5.3 Parts (slots)
One card per slot in `slots[]` order. Slots with `active = false` go to a quiet line at the bottom: «Не нужно для
этого варианта: {label} ({inactive_reason})».

- **Header:** `label`, `hint` (13, `--text-2`), `status_label` chip, qty «× {qty}» when > 1.
- **Options** (`options[]`): radio cards on desktop, a horizontal chip scroller plus the chosen card on phone.
  Selecting one → `PATCH {"choices": {slot: key}}` (with `adjust: true`, the default). The server re-fits the PSU,
  board, RAM and case only where they became incompatible. Show a toast «Поменял и блок питания: 1300 Вт — для двух
  RTX 3090» when other slots changed (diff the previous and new `chosen`).
- **Option card content:**
  - `label` (600) + `source_label` chip only when `source ≠ kb` (blue `bot` «Предложила нейросеть» / neutral
    «Добавлено тобой»).
  - `specs_ru[]` as small chips.
  - Price line: **«рынок ~{price.typical} €»** (per unit; ×qty = `price.total`), then `price.label_ru` in
    `--text-3`, plus `price.range_label` in a tooltip. When `price.rough`, mark it with the dotted `◌` badge.
    `price.typical = null` → «цены пока нет — узнаю из объявлений».
  - **Target:** «цель {target_unit} €» (+ «проверяю до {max_unit} €»). `target_by = "user"` → a pencil chip «твоя
    цель». Editing it → `PATCH {"slots": {slot: {"target_price": 180}}}`, and «Вернуть авто» → `{"auto_target": true}`.
  - `why` (AI or rule reason) in italics, `pros[]` with ✓ (green text), `caveats[]` with ⚠ (amber text) — show 2 and
    hide the rest behind «ещё {n}». `where_ru` in `--text-3` («чаще на eBay…»).
  - `value` (`text_ru`, e.g. «5 € за ГБ видеопамяти · 1 024 ГБ/с») — neutral chip.
  - `new_price` / `new_label` in a tooltip («Новым: ~1 100 € — новой такой нет; ближайшая новая с 24 ГБ — RX 7900 XTX»).
- **GPU slot (`kind = gpu`, LLM builds):** show the options as a **comparison table** (desktop) / stacked cards (phone)
  with columns: Память (`vram_gb` + `vram_status_label` coloured ok/warn/fail), Скорость (`speed.label_ru`, tooltip
  `speed.text_ru`), Видеокарты (`price.total`), Вся сборка (`build_total`; red «над бюджетом» when
  `fits_budget = false`; amber «реально с торгом» when `reachable = true && fits_budget = false`), Ценность
  (`value_score` 0–100 bar + `value_label`), and the first caveat. This table is the answer to «what should I buy for
  my money», so it sits above the other slots.
- **Custom / AI items** (`kind = generic`): one card with `label`, «ищу по словам: {query}», an editable target, and
  a remove icon (`PATCH {"remove_slots": [key]}`). «+ Добавить вещь» → `PATCH {"add_items": [{label, query,
  target_price, qty}]}`.
- **Per-slot menu** (ellipsis): «Уже есть» (`slots: {key: {status: "have"}}`), «Не нужно» (`status: "skipped"`),
  «Снова нужно» (`status: "open"`), «Заметка».

---

## 6. Tracking view (`status = tracking | paused | done`)

The same page, with offers added. Decision first: the **totals bar** leads, then the parts with their best offers.

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ ‹ Сборки  LLM-сервер: 70B Q4_K_M               ● Отслеживаю · 10 поисков   [Пауза] [⋯]  │
│ ┌ totals ──────────────────────────────────────────────────────────────────────────┐  │
│ │ Лучшая сумма сейчас  ~1 005 € из 1 500 €  ██████████░░░░  ◔ Собрано 2 из 8           │  │
│ │ Потрачено 95 € · осталось 1 405 € · новым было бы ~885 € (без видеокарт)            │  │
│ │ Бюджета хватает: цель — брать каждую часть примерно на 10 % дешевле рынка           │  │
│ └──────────────────────────────────────────────────────────────────────────────────┘  │
│ Видеокарты · нужно 2 · цель 190 € за штуку                        Ищу   ▁▂▃▂▁ стабильно  │
│ ┌──────┐ AMD Instinct MI50 32GB HBM2                 175 €   на 15 € ниже цели          │
│ │ img  │ 8 км · 10115 Mitte · сегодня 23:10 · Покупай            5 € за ГБ · 89/100     │
│ └──────┘ ⚠ Пассивная карта: нужен свой вентилятор      [Открыть] [Сделка] [Купил ✓]    │
│   2-я карта: AMD MI50 32GB aus Mining Rig  205 € (+15 € к цели)  ⚠ майнинг              │
│   Ещё: 3 предложения ▾        Варианты: Tesla P40 от 240 € · RTX 3090 — пока нет         │
│ Процессор · Ryzen 5 5600G · цель 85 €                               Ищу                 │
│   Пока нет предложений — жду (первая проверка поиска изучает цены)                      │
│ Блок питания · Куплено за 95 €  ✓                                          [Отменить]   │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

### 6.1 Starting to track
- «Начать отслеживание» → first `POST /projects/:id/track {"dry_run": true}`. Show a confirm sheet:
  **«Создам {created.length} поисков для себя»**, the list of `created[]` names (collapsible), and the load card from
  `estimate` (the same meter as «Поиски», DESIGN_BRIEF §4.5). If the meter is amber / red, add a hint: «Можно следить
  только за главным вариантом — выключи „варианты“» (`alternatives: false`).
  Options in the sheet: toggle «Следить и за вариантами видеокарт» (default on → `alternatives`), advanced: «за
  вариантами всех частей» (`alternatives_scope: "all"`), «Также на eBay» (`ebay: true`, disabled with «Сначала
  подключи eBay» when eBay is not connected), «Искать только в пределах цены» (`price_filter: true`, default off;
  helper: «Меньше объявлений, но цены рынка тогда не изучаются — тренды и „рынок ~…“ станут хуже»), and
  place/radius (prefilled from `location` / `radius_km`, falls back to the user's usual search place).
- Confirm → `POST /projects/:id/track {…same, dry_run: false}` → toast `message_ru` («Слежу за 10 поисками. Первая
  проверка покажет, что уже продаётся, и запомнит цены»). Replace the page with `project`.
- 403 `read_only` → the standard read-only banner (DESIGN_BRIEF §5). 409 → toast with `message_ru`.

### 6.2 Totals bar
- Main number: `best_total` when `best_complete`, else «~{estimated_total}» with the caption «для части деталей —
  по рынку: {missing_offers.join(', ')}». Colour: green when `fits_budget === true`, `--text` when unknown, amber when
  `over_budget > 0`.
- Progress bar = `estimated_total / budget` (clamped; overflow shown red past 100 %), plus a ring with
  `progress_label`.
- Sub-line: «Потрачено {spent} · осталось {remaining_budget}», and `totals.new_label_ru`. When
  `totals.savings_vs_new > 0`, show «б/у выходит дешевле на ~{savings_vs_new}» in green.
- `totals.stretch_label_ru` below.

### 6.3 Part card (tracking)
- Header: `label` · «нужно {need_qty}» (when > 1) · «цель {target_unit} € за штуку» · `status_label` chip ·
  **trend**: a 60×20 sparkline of `trend.points[].median` (weekly medians) + `trend.label_ru` («Дешевеет: −6 % за 2
  недели» green / «Дорожает» amber / «Цена стабильна» / «Мало данных для тренда» in `--text-3`).
- **Best offer** (`best`, a compact deal row from DESIGN_BRIEF §4.2.4): image, `title`, **`unit_cost`** (price +
  shipping; show `price` + «+ {shipping_cost} доставка» when shipping > 0), `vs_target_label` (green when
  `under_target`, amber otherwise), meta row «{distance_km} км · {location} · {first_seen_label} ·
  {verdict_label}», value chip (`value.text_ru`, and for GPUs `value_score`/100 + `value_label`), `flags[]`: `warn`
  → amber ⚠ lines, `info` → neutral chips, `good` → green chips (max 2 visible). `stale` → grey caption `stale_ru`.
  Actions: «Открыть» (`url`, new tab), «Сделка» (`deal_path` → the deal drawer with the German message composer),
  **«Купил»** (§6.4).
- `picked[]` when `need_qty > 1`: the offers that make up `best_cost` («1-я карта…, 2-я карта…»).
- «Ещё {offers_count − picked.length} предложений» expands `runner_ups[]` (compact rows, same actions).
- **Alternatives:** one line «Варианты: {label} от {best.unit_cost} €» per `alternatives[]` with `best`; tapping one
  opens its offers (from `GET /projects/:id/offers?slot=gpu`) and offers «Выбрать этот вариант» → `PATCH choices`.
- No offers yet: «Пока нет предложений — жду». Right after tracking starts, add «первая проверка изучает цены
  (~{interval} мин)».
- Bought / have / skipped: a collapsed row: «Куплено за {spent} €» / «Уже есть» / «Не нужно» + «Отменить»
  (`DELETE …/bought` or `PATCH status: "open"`).

### 6.4 «Купил» dialog
- Title «Купил: {chosen_label}», fields: «Цена» (prefilled with the offer's `unit_cost`, tnum, required, > 0),
  «Сколько штук» (only when `need_qty > 1`; default = 1 when opened from one offer, else `need_qty`), «Заметка».
- Submit → `POST /projects/:id/slots/:slot/bought {"price", "qty", "ad_id", "option", "note"}` (`ad_id` when opened
  from an offer). Response `message_ru` → success toast («Записал: RTX 3090 за 950 €. Осталось 550 € на 5 частей.
  Сделка отмечена как «Купил» в «Моих сделках».») with «Отменить» (6 s) → `DELETE …/bought`. `warning_ru` → amber
  toast. The deal also moves to «Купил» in the pipeline (the backend does it).
- When the last part is bought (`status = done`): a success state for the whole page, «Всё собрано 🎉 · потрачено
  {spent} € из {budget} €», and «Удалить поиски сборки» (they are already paused).

### 6.5 Header actions (tracking)
- «Пауза» / «Продолжить» → `PATCH {"status": "paused" | "tracking"}` (pauses/re-enables the project's searches).
- Overflow: «Изменить бюджет», «Переименовать», «Показать поиски» (→ `/searches`, filtered), «Лучшие видеокарты по
  ценности» (a side sheet with `gpu_ranking[]`: offers across all tracked GPU variants ranked by `value_score`),
  «Удалить сборку» (confirm dialog, §8).

### 6.6 «Уведомления» section
- `alerts_list[]`: time `sent_at_label`, `kind_label` («Ниже цели» / «Сборка укладывается в бюджет»), price, a link
  to the deal (`deal_path`), `delivered_label` («Отправлено» / «Только в приложении» when no channel is set up. Then
  add «Настроить Telegram» → `/settings/notifications`).

---

## 7. Live updates and alerts

- **SSE `project_updated`** (on `/api/v1/events`): `{id, reason, card, ad_id?, slot?, alert?}`.
  - `reason`: `created | updated | tracking | bought | offer | run | alert | deleted`.
  - Update the list card from `card` (no refetch needed). On an open project page, refetch `GET /projects/:id`
    (debounce 1 s) for `offer`, `run`, `alert`, `bought`, `updated`. For `deleted`, navigate away with a toast
    «Сборку удалили».
  - `alert` → toast (green, `boxes` icon): `alert.text_ru` («Сборка «LLM-сервер»: AMD Instinct MI50 32 ГБ за 175 € —
    ниже цели»), action «Открыть» → `/projects/:id`. Bump the nav badge.
- **Telegram / e-mail** (sent by the backend through the normal channels, once per ad):
  ```
  🧩 Сборка «LLM-сервер: 70B Q4_K_M»: AMD Instinct MI50 32 ГБ за 175 €
  Ниже твоей цели 190 € · рынок ~220 € (экономия ~45 €)
  5 € за ГБ видеопамяти · 1 024 ГБ/с
  ⚠ Пассивная карта: нужен свой вентилятор (~25 €)
  8 км от тебя · 10115 Mitte
  https://www.kleinanzeigen.de/s-anzeige/…
  Вся сборка сейчас: ~1 005 € из 1 500 € (для части деталей — по рынку)
  Собрано 0 из 8
  Сборка: http://localhost:8000/projects/1
  ```
  A second kind, «🧩 Сборка «…» укладывается в бюджет», is sent once when the best offers of all parts add up to
  ≤ budget (again after the build stops fitting and fits again). Offers for a tracked **variant** («вместо MI50 —
  RTX 3090 за 580 €») are alerted against that variant's own target.

---

## 8. States matrix

| Screen | Loading | Empty | Error | Partial |
|---|---|---|---|---|
| Сборки (list) | 2–3 skeleton cards | `boxes` + «Собери компьютер из б/у деталей по лучшей цене» + «Скажи, что нужно — я составлю план, проверю совместимость и поймаю каждую деталь дешевле рынка» + primary «Новая сборка» + template chips «LLM-сервер · NAS · Игровой ПК» (each opens `/projects/new?template=…`) | card «Не удалось загрузить сборки» + «Обновить» | — |
| Создание | button spinner; with AI: plan skeleton + «Нейросеть читает цель и выбирает детали…», after 20 s «Можно подождать или продолжить без нейросети» | — | inline field errors (`error.fields`); network → «Не получилось составить план» + «Ещё раз» | AI failed → the rules plan + amber caption `ai.message_ru` |
| План | skeleton hero + 4 slot cards | custom list without items: «Добавь, что нужно купить» + row editor | per-check soft errors are impossible (computed server-side); page error → «Сборка не найдена — возможно, её удалили» + «К сборкам» (404) | `prices_rough` badge; `price.typical = null` → «цены пока нет» |
| Отслеживание | skeleton totals + slot cards | per part: «Пока нет предложений — жду» | toast + retry; 403 read_only banner for track / pause | offers older than 2 days: `stale_ru`; paused project: grey banner «Сборка на паузе — поиски не работают» + «Продолжить» |
| Купил | button spinner | — | 422 inline («Цена должна быть больше нуля»); 409 toast | `warning_ru` amber toast |

**Confirmations** (DESIGN_BRIEF principle 7):
- Delete: «Удалить сборку «{name}»? Её поиски ({searches}) тоже удалятся, найденные объявления останутся в ленте.»
  → «Удалить сборку». A checkbox «Оставить поиски» → `?keep_searches=true`.
- Everything else is instant with «Отменить» (choice changes: re-PATCH the previous `choices`; purchases: `DELETE
  …/bought`; slot status: PATCH back).

---

## 9. API reference (all under `/api/v1`, errors `{error: {code, message_ru, fields?, details?}}`)

### 9.1 Endpoints

| Method + path | Body / query | Returns |
|---|---|---|
| `GET /projects/templates` | — | `{items: Template[], ai: {available, model, label_ru}, quants: [{key,label,bits}], default_quant, default_context, prices_note_ru}` |
| `POST /projects/plan` | PlanRequest | PlanView (nothing saved) |
| `GET /projects` | — | `{items: ProjectCard[], count}` |
| `POST /projects` (201) | `{plan?: PlanView.plan, choices?: {slot: option}, name?, budget?, location?, radius_km?}` or PlanRequest fields | ProjectView |
| `GET /projects/:id` | — | ProjectView |
| `PATCH /projects/:id` | `{name?, goal?, budget?, clear_budget?, status?: "tracking"\|"paused", choices?: {slot: option}, adjust? (true), slots?: {slot: {status?: "open"\|"have"\|"skipped", target_price?, auto_target?, note?, qty?}}, add_items?: CustomItem[], remove_slots?: [slot], location?, radius_km?}` | ProjectView |
| `DELETE /projects/:id` | `?keep_searches=false` | `{deleted, searches_removed: [names], message_ru, warning_ru}` |
| `POST /projects/:id/track` | `{alternatives? (true), alternatives_scope?: "key"\|"all", max_alternatives? (2), slots?: [slot], location?, radius_km?, ebay? (false), price_filter? (false), dry_run? (false)}` | `{created, updated, paused, tracked, estimate, searches, dry_run, message_ru, project?}` |
| `POST /projects/:id/slots/:slot/bought` | `{price, qty?, ad_id?, option?, note?}` | `{project: ProjectView, message_ru, paused_searches, warning_ru}` |
| `DELETE /projects/:id/slots/:slot/bought` | — (undo the last purchase) | `{project, message_ru}` |
| `GET /projects/:id/offers` | `?slot=&limit=10` | `{id, count, slots: [{slot, label, option, option_label, target_unit, need_qty, status, offers: Offer[], alternatives: [{option, label, offers}]}], gpu_ranking: Offer[]}` |

**PlanRequest:** `{goal (≤2000), budget?, template?: llm_24|llm_48|nas|gaming_1080p|custom, name?, use_ai (true),
model_size_b?, active_b?, quant?, context?, vram_gb?, storage_tb?, drives?, location?, radius_km?, items?:
[{label, query?, target_price?, qty?}]}`. Unknown fields → 422.

### 9.2 PlanView (preview) = the plan part of ProjectView
`name, goal, template, template_label, icon, kind (llm|nas|gaming|custom), budget, requirements {kind,
model_size_b, active_b, quant, quant_label, context, vram_gb, weights_gb, kv_cache_gb, storage_tb, drives,
lines_ru[]}, slots: Slot[], checks: Check[], checks_summary {fail, warn, status, text_ru}, totals: Totals,
summary_ru, notes[], ai {used, ok, model?, summary_ru?, message_ru, applied?, ignored?}, location, radius_km,
prices_rough, prices_note_ru, plan (opaque: send back to POST /projects)`.

- **Slot:** `key, label, kind, hint, active, inactive_reason, status, status_label, qty, bought_qty, need_qty,
  chosen, chosen_label, options: Option[], note, purchases [{price, qty, ad_id, option, note, at, at_label}], spent,
  searches [names]` + (always) `price, target_unit, max_unit, target_total, best, picked[], runner_ups[],
  offers_count, best_cost, best_complete, alternatives [{option, label, best, count}]` + in ProjectView `trend
  {points [{date, median, n}], change_pct, direction: down|up|flat|unknown, label_ru}`.
- **Option:** `key, kb_key, label, unit_label, qty, kind, chosen, source (kb|ai|user), source_label, query, specs,
  specs_ru[], pros[], caveats[], where_ru, why, price {typical, low, high, source: history|kb|none, label_ru, rough,
  sample_size, new, new_label_ru, notes_ru, qty, total, range_label?}, target_unit, max_unit, target_by (auto|user),
  new_price, new_label, value {key, label_ru, value, text_ru} | null, risk` + GPU: `value_score, value_label, vram_gb,
  vram_status (ok|warn|fail), vram_status_label, speed {ceiling, low, high, label_ru, text_ru}, build_total,
  fits_budget, reachable`.
- **Check:** `key, status (ok|warn|fail|info), status_label, title, summary, lines[], fix {slot, option, label_ru} | null`.
- **Totals:** `budget, spent, remaining_budget, typical_total, unknown_price_slots[], target_total, best_total |
  null, best_complete, estimated_total, missing_offers[], fits_budget (true|false|null = not every part has an offer
  yet), fits_estimate, over_budget, new_total, new_missing[], new_label_ru, savings_vs_new, slots_total, slots_done,
  ratio, stretch_label_ru`.

### 9.3 ProjectCard (list, SSE) and ProjectView
**ProjectCard:** `id, name, goal, template, template_label, icon, status (draft|tracking|paused|done),
status_label, budget, spent, remaining_budget, best_total, best_complete, estimated_total, fits_budget,
fits_estimate, over_budget, total_label («~1 005 € из 1 500 €»), slots_total, slots_done, progress (0..1),
progress_label, searches (count), alerts (count), last_alert_at, last_alert_at_label, created_at, updated_at,
updated_at_label, tracking_since, tracking_since_label, next_step {key: track|buy|resume|wait|done, label_ru, slot?},
headline_ru`.

**ProjectView** = ProjectCard + PlanView + `searches_list [{name, slot, option, exists, enabled, id}]` (`id` = the
search id for `/searches/:id`), `alerts_list [{kind, kind_label, ad_id, slot, price, total, text, delivered,
delivered_label, sent_at, sent_at_label, deal_path}]`, `gpu_ranking: Offer[]`.

**Offer:** `ad_id, title, url, image, source, source_label, price, price_text, shipping_cost, unit_cost, negotiable,
distance_km, location, first_seen, first_seen_label, last_seen, last_seen_label, stale, stale_ru, verdict,
verdict_label, action, action_label, score, market_price, savings, status, status_label, under_target, vs_target,
vs_target_label, flags [{key, level: warn|info|good, text_ru}], value, value_score, value_label, option, slot,
deal_path, search_name`.

Offer flag keys: `mining`, `many` (seller has several cards), `water` (water block), `blower`, `open_cooler`, `fe`
(Founders Edition), `rtx3090_memory`, `rtx3090ti`, `passive`, `eps`, `fan_included`, `rocm`, `mi50_16`,
`no_display`, `no_cables`, `rdimm`, `smr`.

---

## 10. Copy

**Buttons:** «Новая сборка» · «Составить план» · «Сохранить и начать отслеживание» · «Сохранить черновик» · «Начать
отслеживание» · «Пауза» · «Продолжить» · «Купил» · «Уже есть» · «Не нужно» · «Снова нужно» · «Отменить покупку» ·
«Выбрать этот вариант» · «Показать расчёты» · «Вернуть авто» · «Удалить сборку» · «Добавить вещь».

**Toasts:** «Сборка сохранена» · `track.message_ru` · `bought.message_ru` · «Поменял и {slot}: {option} — {reason}» ·
«Сборка на паузе — поиски не работают» · «Снова слежу за сборкой» · `alert.text_ru` (action «Открыть»).

**Helpers:**
- Target: «Цель — сколько ты готов заплатить за эту часть. Я считаю её так, чтобы вся сборка уложилась в бюджет, и
  минимум на 10 % ниже рынка».
- «проверяю до …»: «Объявления до 30 % дороже цели тоже проверяю — с продавцом можно поторговаться. Дороже — только
  запоминаю цену, чтобы знать рынок».
- Ориентир: `prices_note_ru`.
- Value score: «Ценность для нейросети: сколько видеопамяти и скорости получаешь за каждый евро (RTX 3090 по рынку = 50)».
- Speed: «Грубая оценка: каждый токен читает все веса модели из памяти видеокарты; реальная скорость — примерно половина от потолка».

**Glossary for tooltips:** VRAM → «видеопамять: в неё должна влезть модель целиком»; Q4_K_M → «сжатие модели до ~4,85
бита на параметр — почти без потери качества»; KV-кэш → «память под контекст разговора»; ток/с → «сколько слов в
секунду пишет модель (1 токен ≈ ¾ слова)»; Above 4G Decoding → «настройка в BIOS, без которой система не видит
серверную видеокарту».

---

## 11. Notes for the frontend

- Money: format with the app's usual money formatter (the API sends plain numbers, EUR). Text fields (`*_ru`,
  `total_label`, `lines[]`) are ready to show; don't reformat them.
- Never compute prices or compatibility client-side. Send `choices` / `slots` and render what comes back. The one
  local computation allowed is the diff of `chosen` values, to say which other slots changed.
- `plan` in the preview is opaque: keep it in memory and post it back unchanged.
- A project search is a normal search: editing its words or place in «Поиски» is fine. The project only updates its
  price limits (target / max) and enabled state.
- The AI never produces prices. An AI-suggested item (`source = "ai"`) starts with `price.typical = null`; show «цены
  пока нет — узнаю из объявлений».
