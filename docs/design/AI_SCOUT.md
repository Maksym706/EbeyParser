# AI scout («ИИ-разведчик») — AI-first deal finding, data-driven money

**Status:** implemented (phase 1 modules + phase 2 integration, 2026-09-29). Code: `ebeyparser/ai/triage.py`,
`ai/prompts_triage.py`, `ai/scout.py`, `pricing/ai_key.py`, `pricing/bundle.py`, `pricing/tiers.py`, the scout
parts of `monitor.py`, `db.py` and the web API. Benchmark: `ebeyparser/benchmark_gems.py`.
Model choices per hardware: `ebeyparser/ai/model_catalog.py` / `docs/design/AI_MODELS.md` (separate work).

## 1. Why

Finding the best items to buy is the product, and a script can't do it alone. Before the scout, the AI was a late
verifier. Keywords and the regex identity layer (`pricing/identity.py`) decided which ads were even looked at. Price
history dropped "no deal" ads early. The local vision model saw at most 30 survivors per pass. So the AI never saw what
the script dismissed or could not read:

* badly titled ads, typos (`Iphne 13 128gb`, `Playstaion 5`), sellers who don't know what they have
  («Grafikkarte von Nvidia, weiß nicht welche»);
* bundles and lots («Konvolut», «Nachlass», «Dachbodenfund»), consoles with extras;
* «alter PC» with a valuable GPU inside (identity kind `complete_pc` → free kind veto);
* wrong categories (a DJI Mini 3 under «Haushalt» as «Spielzeug Drohne»);
* anything the regexes can't identify (no identity key → no history → no price).

The scout inverts the order. **The AI reads every new ad first and says what it really is. The money comes from real
data only. The hard safety rules stay exactly as they were.**

Three principles:

1. **The AI finds, data prices.** The triage model is never asked for a price. 1.5–7B models invent prices. Every euro
   comes from our own price history or live comparables.
2. **Grounded or it didn't happen.** A product, part or variant the model names counts only if it is really written in
   the ad and not negated («ohne Grafikkarte», «RTX 3080 ausgebaut», «suche … RTX 3080»). This check is the guard
   against a small model's inventions (`pricing/ai_key.grounded`).
3. **The scout can only add.** When it is off, down, slow or confused, the pipeline behaves as before. It may promote a
   dismissed ad or rank candidates. It can never bypass keywords, price ranges, wanted/swap/defect rules, red flags,
   the vision veto rules or the profit math.

## 2. Pipeline

```
scrape → store → remember prices
  │
  ├─ A  SCOUT READ (text-only, batched, bounded time)      ai/triage.py
  │     new ads, fresh first, then where the script is blind
  │     → per ad: kind, product, qty, contents, query, condition, hidden-value tags, risk tags,
  │       interest 0..10, reason (ru)          [overflow → script path, nothing blocks]
  │
  ├─ B  PRICE WHAT IT READ (free: DB only)                  ai/scout.plan, pricing/ai_key, pricing/bundle
  │     ground in ad text → identity key or "ai:" key → history estimate;
  │     bundle/pc/lot = Σ priced parts × (1 − discount), only if enough model parts priced
  │
  ├─ free funnel (_triage): script prefilter / kind veto / history early-skip as before, plus:
  │     • kind-vetoed PC/bundle/lot, or early-skipped where the scout saw ANOTHER product,
  │       whose plan shows a possible deal  → PROMOTED  (found_by = "ai_scout")
  │     • script candidate with unknown market → gets the scout's price (found_by = "ai_scout" if the
  │       scout identified a different product) or its interest as rank; junk ranks last
  │
  ├─ C  PAID: ad page → re-ground on the FULL text (drops «ohne Grafikkarte» cases) →
  │     comparables via the scout's key/phrase or for ≤ 2 unpriced bundle parts →
  │     VISION check (existing _ask_ai; PC/bundle item types are no veto when the scout expects them;
  │     no comparables shown for part-priced bundles; product disagreement caps at "maybe")
  │
  └─ D  DECISION: existing evaluate() (fees, profit, ROI, every hard rule) → tier (super/deal/…) →
        alert: «🔥 Супер-находка» at once past the hourly cap; others as before; «Топ за день» digest.

end of pass: SCOUT RESCUE — scout time left over → read recent dismissed ads it didn't reach, re-plan
             stored interesting readings (history grows) → promote → paid stage.
start of pass: VISION QUEUE — would-be deals whose photos the offline PC couldn't check.
```

### Two AI endpoints

The home server runs the app 24/7. It is weak: CPU only, 4–16 GB RAM, maybe ARM. The gaming PC with a GPU is on only
sometimes (LAN/Tailscale).

| role | runs on | model | config |
|---|---|---|---|
| **scout** (stage A) | the always-on server's CPU | 1–4B text model, e.g. Qwen3.5 2B / 4B (see catalog) | `ai.scout.*` (own `base_url`, `model`) |
| **vision** (stage C) + second look | the PC when it's on | 7B+ vision model | `ai.*` |

An empty `ai.scout.base_url`/`model` means the scout uses the `ai` endpoint.
**When the vision endpoint is offline**, a candidate the math calls a deal is stored as `would_buy` with
`ai_checked=False` and put into `vision_queue`. Each pass starts by draining the queue. If the model is back, it checks
the photos and the deal alerts normally (`deal_updated` event). After `ai.vision_wait_minutes` (default 45), the deal
goes out marked «⚠ ФОТО НЕ ПРОВЕРЕНЫ ИИ — проверь сам» (the existing `notifications.unchecked_deals` rule). Scraping
never waits for it.

## 3. The triage prompt (small-model friendly)

`ai/prompts_triage.py`. The design targets 1.5–4B models on a CPU; a 7B only gets better.

* **One flat object per ad, one-letter keys, enums**: `i, k, p, n, c, q, z, h, x, s, r`. Nesting and free text are
  where small models derail; one-letter keys also cut output tokens by ~40 %. Output tokens are the whole cost on CPU.
* **Index-based**: `{"items":[{"i":0,…},…]}`. Items are matched by `i`, never by order. They go by order only when the
  count matches exactly and indexes are missing.
* **Facts first, judgement last**: kind → product → contents → query → condition → tags → interest → reason
  (generation order = reasoning order).
* **No prices anywhere.** `interest 0..10` is "is this a resellable, valuable thing?" General knowledge ("an RTX 3080
  is valuable") is fine; a euro number is not asked for.
* **One worked example** with three ads (old PC with RTX 3070, typo'd iPhone, wanted ad) in the system prompt. The
  system prompt is identical for every call, so llama.cpp / Ollama / LM Studio keep it in the prompt cache, and after
  the first call it costs nothing.
* **Input per ad** (`ad_block`): `[i] Titel | Preis (VB / zu verschenken / Auktion) | Kategorie | eBay | Text` (≤ 320
  characters: the search card only has ~150 anyway).
* **Strict JSON schema** (closed objects, all keys required, enums) for llama.cpp grammars / LM Studio / vLLM. It goes
  through the existing `VisionLLM` chain `json_schema → json_object → plain`.
* **User preferences** from feedback are appended (`feedback_hints`, ≤ 600 characters), see §9.
* Adopted from the model research's CPU measurements (`AI_MODELS.md` §5):
  * **minified one-line JSON** is demanded, since schema grammars allow whitespace and indentation was over half the
    output tokens;
  * **`single` is the explicit default kind**, and the other kinds list their German trigger words (Suche/Kaufe,
    Tausche, für Bastler / iCloud gesperrt, nur OVP);
  * model numbers and sizes are **copied verbatim**;
  * **thinking is off** for the scout's calls (`make_scout_llm`: `chat_template_kwargs.enable_thinking=false`,
    Ollama `think=false`);
  * batches of 8–10 are right for ≥ 2B; ≤ 5 is better for smaller models (`ai.scout.batch_size`).

Keys: `k` kind ∈ single·bundle·pc·lot·part·acc·box·wanted·swap·service·defect·other; `p` "Brand Model Variant
Storage" or ""; `n` quantity; `c` contents of bundle/pc/lot ("2x DualSense Controller"); `q` German search phrase;
`z` condition; `h` hidden-value tags (typo, vague, unknown_model, pc_parts, lot, bundle, wrong_category, cheap); `x`
risk tags (scam, defect, locked, fake, missing, reserved, rent); `s` interest; `r` reason in Russian, ≤ 8 words.

### Robustness (`parse_triage`, `TriageEngine`)

| failure | handling |
|---|---|
| markdown fences, `<think>`, prose around JSON, trailing commas, Python-style literals | stripped/repaired |
| truncated answer (max_tokens, looping) | every complete item object is kept (string-aware brace scan) |
| bare list, `{"0": {...}}`, aliases (`index`, `type`, `produkt`, `interest: "8/10"`, `0.7`) | normalised |
| missing / duplicate / out-of-range / shifted `i` | the item is dropped (never guessed). A shifted index gives an item whose product isn't in that ad's text, so grounding rejects it |
| ads missing from the answer | retried in halves, then singly (2 levels); still failing → `script_item` (identity's reading, `source="script"`, never promotes) |
| server down / refused (`LLMError`) | the run stops, the rest is overflow (script path), one health alert «Нейросеть-разведчик не отвечает…» per 6 h |

**Adaptive batch** (default 8, 2–16): halves when < 70 % of a batch parses, grows by 2 after 3 full successes. It is
also capped so one call stays under `min(120 s, 0.6 × timeout)` at the measured seconds/ad, and so the answer fits
`max_tokens` (110 tokens/ad + 60).

## 4. Throughput: can the model keep up?

**Arrivals (M, new ads/hour)** in a typical Berlin setup (30 km radius). These are estimates from Kleinanzeigen volume
(roughly 20–30 k new Berlin ads/day, ~15–20 % electronics, evening peak ≈ 2× average), and they match what the
monitor itself sees per pass:

| setup | average | evening peak |
|---|---|---|
| 1 category scan (e.g. «Handy & Telefon») | 40–80 | 100–150 |
| typical student setup: 4–6 category scans + keyword searches | 100–200 | 250–400 |
| whole «Elektronik» tree | 250–400 | 500–800 |

The app's own caps also bound M: `general.max_requests_per_hour` (150 pages ≈ 3,750 cards) and 3 pages per category
per pass.

**Capacity (C, ads/hour)** of batched triage: ~110 prompt + ~70 output tokens per ad, system prompt cached, Q4_K_M,
llama.cpp. Output tokens dominate on CPU. The table shows 100 % duty and the default `pass_share` 0.5:

| hardware | model (Q4_K_M) | s/ad | C @100 % | **C @50 %** |
|---|---|---|---|---|
| RTX 3090 24 GB | Qwen3.6 35B-A3B MoE / 7–9B | ~0.5 | ~7,000 | **~3,500** |
| RTX 3060 12 GB / 4060 8 GB | Qwen3.5 9B / 7B | ~1.3–1.5 | ~2,500 | **~1,200** |
| 8-core CPU, dual-channel RAM (T1) | Qwen3.5 2B / 4B | ~2.5 / ~6 | 1,400 / 600 | **700 / 300** |
| 4-vCPU VM ≈ N100 (T0), **measured** | Qwen3.5 2B / 4B | **~6 / ~15.5** | 600 / 230 | **300 / 115** |
| 4-core CPU (T0) | 7B | ~15–20 | ~200 | **~100** |

The CPU rows are the model research's measurements on this sandbox (`AI_MODELS.md` §4, `model_catalog.MODELS[*]
.sec_per_ad`): batched triage, strict schema, llama.cpp. For quality, Qwen3.5 4B read product 97 %, kind 87 %,
scam 100 % and JSON 100 % on that test set; 2B read product 60 %, kind 63 %, JSON 100 %. The GPU rows are
generation-speed extrapolations. Output tokens dominate on CPU: the minified, one-letter-key format is worth ~2–3×.

**Conclusions**

* Any GPU reads every ad of any realistic setup with a 5–20× margin, so the scout could run on the PC. But the PC is
  not always on, which is why the scout has its own endpoint.
* A **4-core box with Qwen3.5 2B (~300/h at 50 %)** keeps up with a typical setup (100–200/h average) and almost
  with its evening peak (250–400/h). With the better-reading 4B (~115/h) it needs "candidates" mode at peak. An
  8-core box with 4B (~300/h) mostly keeps up.
* `ai.scout.mode: "auto"` (default) switches to **"candidates"** when the last hour's arrivals exceed the capacity
  (`ScoutStats.capacity_per_hour(pass_share, max_per_hour)`). In that mode the scout only reads the script's blind
  spots (`triage.blind_spot`: no identity key or a catch-all key, PCs, bundles, lots, vague titles, free ads). That is
  ~30–40 % of a stream, so a T0 box copes even at peak. The ads it skips go the script path, and the end-of-pass
  **rescue** reads recent dismissed ads when time is left.
* Hard limits keep a 4-core box usable: `ai.scout.max_per_hour` (600) and `ai.scout.pass_share` (0.5 of
  `general.interval_minutes`, shared fairly between the searches of a pass; unused time rolls over).

## 5. Stage B: pricing what the scout read

`pricing/ai_key.py`:

* `resolve(name)` gives the **identity.py key** when identity recognises the name with a real category
  (`"Apple iPhone 13 Pro 256GB"` → `iphone|13|pro|256gb`), so the scout and the script share one history. Otherwise
  it gives a **normalised AI key** `ai:<model words>|<sorted attributes>` (brand, colours, filler dropped):
  `ai:soundlink flex`, `ai:anker powercore|20000mah`. identity's catch-all "word + number" keys (`fenix|7`,
  `thermomix|tm6`) are used as they are: the scout writes canonical names, so they are the keys the script's history
  already fills. (Using a separate `ai:` key there left the scout with 5 prices where the script had 42.) A typo in
  the *title* (`iphne|13`) still counts as "the script can't read it" (`Monitor._scout_sees_other`), so the AI's
  corrected product wins.
* `grounded(product, ad_text)`, per clause (a negation never crosses a comma):
  * every strong model token must be present, exactly (`3080` ≠ `3070`). A series marker is optional (`i7` in
    `i7-8700K`).
  * a bare short number (`7`, `13`) also needs a product-line word, where one typo is allowed for words of ≥ 5
    letters (`Iphne`, `Samsnug`, `Playstaion`).
  * variant words the model adds (Pro, Max, Ti, OLED, Digital, Air …) and storage sizes must be written in the ad.
  * negated when a clause has «ohne/kein/nicht/fehlt/ausgebaut/entfernt» before it, «fehlt/ausgebaut/verkauft/defekt/
    nicht» after it, «war … drin/verbaut», or a want-word («suche/statt/wäre»).
* Prices come from history only: identity keys via `price_history_spans` + `comparable_fits` (same product and kind,
  like the script); AI keys via `price_history_keyed` + attribute compatibility.
* **The history grows under the scout's keys.** When the script's identity knows nothing (no key or a catch-all key), a
  fresh, grounded, single, risk-free ad the scout identified is stored under the scout's key (identity or `ai:`). The
  price point row (one per ad) is re-keyed, so `iphne|13` never collects prices.

`pricing/bundle.py` (`value_bundle`) — bundle / PC / lot = Σ(qty × market of grounded, priced parts) × (1 − discount).
The discount is `bundle_discount` 0.15, or `pc_discount` 0.25 for PCs and lots (parting out is work). Parts without a
price count as 0, so the value is a **lower bound**. It is only given when at least `min_priced_share` (0.5) of the
**model-numbered** parts are priced. RAM/SSD/PSU/case/cables are "minor" (their numbers are sizes) and never block.
The estimate is shaped like any other (`source="history"`/`"mixed"`, `sample_size` = thinnest part, low/high sums).
So `evaluate()`'s thin-data and spread rules apply to it unchanged.

No data means no price. A single the history doesn't know gets one comparables lookup in the paid stage with its
identity key (exact matching) or, for AI keys, with the scout's German phrase. A bundle gets lookups for ≤ 2 unpriced
model-numbered parts. With no market price at all, the old rule holds: the vision model's guess makes it at most
«maybe», `no_alert`.

## 6. Promotion rules (`ai/scout.worth_a_look`, `Monitor._triage`)

A dismissed ad comes back only if **all** of these hold:

* the reading is a real AI answer (`source="ai"`) and not blocked: its kind is not in `wanted, swap, service, box,
  defect, other, acc, part`, and it has no risk tag in `scam, locked, fake, rent, defect, missing, reserved`;
* the product, or at least one bundle part, is grounded;
* the dismissal was one of two things, and nothing else:
  * a **kind veto the scout may overrule** (`SCOUT_OVERRULES`): `complete_pc` only when the scout read the ad as a
    PC/lot/bundle priced by its parts, `laptop` only as an identified single product. `part` (missing components,
    «Switch nur Tablet»), `accessory`, `defect`, `box_only`, `wanted`, `swap` and `service` are never overruled;
  * an **early skip** where the scout saw a different product (`_scout_sees_other`).

  Stop words, include keywords, price ranges, `min_listing_price` and severe flags are never overruled either;
* the math on the scout's price says "possible deal" (`evaluate()` without AI is not a no-deal). Or there is no price
  yet and the interest is high: ≥ `min_interest` for identified singles (a comps lookup follows), ≥ `min_interest+1`
  for bundles with unpriced model parts, ≥ `min_interest+2` for bundles with no priced part.

The paid stage re-grounds on the full ad text. If the reason is gone («ohne Grafikkarte» further down), the script's
original decision is stored.

**The scout never drops an ad on its own.** A script candidate whose scout price says "no deal" (a weak model may have
read a cheaper variant) is ranked after the unknown ones but keeps the script's own path. Junk readings (interest ≤ 2,
blocked kinds) only rank last. **A PC/bundle/lot is never priced by the vision model's search phrase**, which names one
part («rtx 3070») and would price the whole thing as that part. Without real part prices it stays at most «maybe».

## 7. Stages C/D

* `vision_for_plan`: a PC/lot/bundle the scout read as one is expected to be one, so the vision model's
  `complete_pc`/`bundle`/`laptop` item type is mapped to `bundle`. `part`, `accessory`, `box_only` and `wanted` still veto.
* A part-priced bundle shows the vision model **no comparables**, since the parts are not "the same variant" as the
  whole and `ai_variant_matches=0` would wrongly cap it.
* `vision_disagrees`: the photo check names a different model than the scout, so the verdict is capped at «maybe»
  with a warning.
* `found_by = "ai_scout"` (new `Evaluation.found_by`) when the scout promoted the ad, gave it its market price, or its
  key/phrase found the comparables. A reason line «🔎 Нашла нейросеть: …» is added.
* **Alert tiers** (`pricing/tiers.py`, `notifications.super_deals`):
  * `super` means "buy" with profit ≥ 120 € **and** ROI ≥ 80 %, score ≥ 85, a market from real data, photos checked,
    no flags at all. It is sent at once, **past the hourly cap and the digest mode**, with the headline
    «🔥 Супер-находка: … за … → прибыль ≈ …» (Telegram shows it as a header, e-mail as the subject).
  * `deal`, `unchecked`, `maybe`, `skip` follow the current rules.
* **«Топ за день»** (`notifications.daily_top`, off by default): at `hour` (general.timezone), the best `per_search`
  deals of 24 h per search in one message, including ones already sent.

## 8. Safety: why precision stays ~100 %

Every «buy» still passes `evaluate()`: real market data, fees, profit and ROI thresholds, thin-data and spread caps,
too-cheap/bait rules, severe red flags, and the vision check (defects, lock, stock photos, wrong item type). On top of
that the scout adds grounding, blocked kinds/risks, the full-text re-check, the lower-bound bundle value and the
disagreement cap. Scout failure modes and what stops them:

| scout mistake | stopped by |
|---|---|
| invents a pricier model/variant/storage («3070» → «3080», «13» → «13 Pro», «128» → «256») | grounding (exact tokens, variant words, storage) |
| lists a removed part («ohne Grafikkarte (RTX 3080 ausgebaut)») | clause negation; full-text re-grounding |
| reads a wish as content («suche eigentlich was mit RTX 3080») | want-word negation |
| misses a defect/scam/lock | the script's own red flags on title+text, the vision veto, too-cheap/bait rules (unchanged) |
| broken JSON / wrong index | parser; grounding rejects a shifted item |
| over-values a bundle | lower-bound valuation, pc/bundle discount, thin-data caps |
| misses «nur Tablet» / «ohne Akku» (missing parts) and names the full product | the `part` kind veto is never overruled |
| an unpriced PC that the vision model prices by one part | no vision-phrase pricing for PC/bundle/lot plans |
| under-claims a variant (cheaper model) → "no deal" | the scout never drops an ad; the script path still runs |

## 9. Learning loop

Implemented: `Database.feedback_examples()` → `scout.feedback_hints()` adds ≤ 600 characters to every batch prompt.
Up to 3 recent bought/sold deals («good buy: Alter PC mit RTX 3070 (bought 150 €, sold 390 €)») and up to 4 hidden
ones with the hidden reason («not interesting: Kinderwagen (не интересно)»). The model rates similar ads accordingly.
`ai.scout.learn_from_feedback` switches it off.

Next steps:

1. Per-category/kind **preference weights** from hidden reasons ("не интересно" × kind/category), applied to the
   interest in code, not in the prompt.
2. **Realised profit** per kind/category (bought/sold prices in `deal_state`) raises `min_interest` where the user loses
   money.
3. Few-shot examples picked by **similarity** (embeddings, see the model catalog's `embed` task) instead of recency.

## 10. Configuration (all additive, defaults in brackets)

`ai.scout` (`ScoutConfig`, an `LLMSettings`):

| field | meaning |
|---|---|
| `enabled` [false] | the scout on/off |
| `provider` [openai], `base_url` [""], `model` [""], `api_key` | its own endpoint; empty means the `ai` endpoint |
| `timeout_seconds` [180], `temperature` [0.1], `max_tokens` [1800] | one batch call |
| `mode` [auto] | `auto` \| `all` \| `candidates` (see §4) |
| `batch_size` [8], `min_batch` [2], `max_batch` [16] | adaptive batch |
| `max_per_hour` [600] | hard cap of ads read per hour |
| `pass_share` [0.5] | share of the check interval the scout may use per pass |
| `min_interest` [6] | the promotion threshold (see §6) |
| `backlog_hours` [6] | how long dismissed ads stay eligible for the rescue |
| `bundle_discount` [0.15], `pc_discount` [0.25], `min_priced_share` [0.5] | bundle valuation |
| `learn_from_feedback` [true] | preference hints in the prompt |

Plus `ai.vision_wait_minutes` [45]; `notifications.super_deals` {enabled [true], min_profit [120], min_roi [0.8],
min_score [85]}; `notifications.daily_top` {enabled [false], hour [20], per_search [3], verdicts [buy, maybe]}.
Settings API: ranges added, `ai.scout.api_key` is secret (read-only), and `ai.scout.base_url` must be http(s).

## 11. Database (additive; `CREATE TABLE IF NOT EXISTS`, safe on old files)

* `scout_triage(ad_id PK → listings ON DELETE CASCADE, triaged_at, source, kind, interest, product, data JSON)`: each
  ad is read once; readings survive restarts; `scout_backlog()` / `scout_counts()` build on it.
* `vision_queue(ad_id PK → listings ON DELETE CASCADE, search_name, queued_at)`.
* `price_points.product_key` also holds `ai:` keys. It is free text, so there is no migration; `price_history_keyed()`
  returns the stored key.
* `evaluations.data` JSON has `found_by`. `RunSummary` has `scout_read, scout_overflow, scout_failed, scout_calls,
  scout_seconds, scout_promoted, scout_deals, super_deals, vision_waiting`.
* `kv_state["scout:stats"]` is the last throughput snapshot (the UI after a restart); `kv_state["daily_top"]`.
* `reset_all`, `delete_listings` and retention cover the new tables.

## 12. API and UI hooks

Backend (done):

* **Deal card** (`presenters.deal_card`): `found_by` ("script" | "ai_scout"), `found_by_label` («Нашла нейросеть»),
  `scout_reason` (the scout's one line), `tier` (super|deal|unchecked|maybe|skip), `tier_label` («🔥 Супер-находка» …).
* **`GET /api/v1/monitor`** → `scout`: `{enabled, state (off|idle|ok|behind|down), text_ru («Успевает смотреть 90 из
  120 новых объявлений в час»), speed_ru («≈ 5.0 с на объявление, до 360 объявлений в час»), mode, mode_setting,
  mode_ru, provider, base_url, model, own_endpoint, seen_last_hour, read_last_hour, overflow_last_hour,
  failed_last_hour, sec_per_ad, capacity_per_hour, batch_size, vision_queue {waiting, max_wait_minutes, text_ru}}`.
* **`GET /api/v1/health`**: the same `scout` block. The banner warns when the scout is down or deals wait for the photo
  check.
* **`GET/PATCH /api/v1/settings`**: `ai.scout.*`, `ai.vision_wait_minutes`, `notifications.super_deals.*`,
  `notifications.daily_top.*`.
* **`POST /api/v1/ai/scout/test`**: runs the triage on 4 built-in sample ads (hidden RTX 3070 in an old PC, a typo, a
  wanted ad, a pram) → `{ok, seconds, sec_per_ad, per_hour, answered, hidden_gpu, typo_fixed, wanted_seen, items[],
  message_ru, error_ru, suggested_model}`. It picks the catalog's best installed triage model when none is set;
  `save=true` stores the endpoint and switches the scout on.
* **Events**: `deal_found` / `deal_updated` (from the monitor) keep their old keys and add `found_by`, `tier`,
  `listing` (JSON), `evaluation` (JSON) and `card`.

Frontend (for the SPA team, `ebeyparser/web/app/**`):

1. **«Нашла нейросеть» badge** on deal cards and the deal page when `card.found_by == "ai_scout"`, tooltip = `card.scout_reason`.
2. **«🔥 Супер-находка»** style (accent border/ribbon) when `card.tier == "super"`, and a feed filter «Только супер».
3. **Settings → Нейросеть**: a «Разведчик» block with an on/off switch; a mode segmented control
   «Авто / Все объявления / Только непонятные» (`ai.scout.mode`); an endpoint (address + model, default «как у
   нейросети для фото»); a «Проверить разведчика» button (`POST /ai/scout/test`, showing the 4 sample readings and
   «≈ N объявлений в час»); the live line `scout.text_ru` + `scout.speed_ru`; an advanced section (batch, cap/hour,
   share of interval, min interest); «Ждать проверку фото, мин» (`ai.vision_wait_minutes`).
4. **Состояние**: a «Разведчик» tile with `scout.text_ru`, `speed_ru`, `mode_ru`, and «не успел N» (`overflow_last_hour`);
   a «Ждут проверки фото: N» line (`scout.vision_queue.text_ru`).
5. **Settings → Уведомления**: «Супер-находки сразу» (enabled + min profit/ROI) and «Топ за день» (enabled, hour, per search).

## 13. Monitor integration (what changed where)

* `__init__(…, scout=TriageEngine)` for tests/benchmarks; `_ensure_components` builds the engine from `ai.scout`
  (`_scout_llm_settings` falls back to `ai`).
* `run_once`: `_scout_begin_pass` (time budget), `_drain_vision_queue`, per search `_scout_read` (before the free
  funnel), `_scout_rescue` after all searches, `_scout_save_stats`.
* `_triage` (free funnel): `_scout_plan_for`, promotion via `_scout_candidate` (kind veto / early skip), `_scout_enrich`
  (candidates: price, rank, scout early skip).
* `_Candidate`: `plan`, `found_by`, `priority`, `script_skip`; `discount` uses the scout's priority when the market is
  unknown.
* `_finish`: full-text re-grounding (back to `script_skip`), `_scout_price` / `_component_estimate`, `found_by`.
* `_conclude` / `_ask_ai`: `vision_for_plan`, no comparables for part-priced bundles, `vision_disagrees`, the reason
  line, `_hold_for_vision`.
* `_after_evaluation`: counts, the `deal_found` payload (`_deal_event`), tier-aware alerts; `_alert(super_find=True)`
  skips the cap; `_should_notify` holds queued would-be deals; `_maybe_daily_top` in `_after_run`.
* `scout_status()` for the API.

## 14. Evidence

**Benchmark** (`python -m ebeyparser.benchmark_gems --seed 1 --n 400`). This is the standard synthetic Berlin market
(deals, normal ads, all traps) plus 12 % hidden gems and 5 % gem-shaped traps. The real Monitor runs with the scout
off and on. The scout's model is simulated as JSON text through the real engine, at three qualities, with simulated
GPU/CPU speed. See §14.1.

**Real LLM smoke test**: Qwen2.5-1.5B-Instruct Q4_K_M on this 4-core sandbox CPU via llama.cpp `llama-server`
(OpenAI-compatible) on 40 realistic German ads. See §14.2.

### 14.1 Benchmark results

`python -m ebeyparser.benchmark_gems --seed S --n 400` for S = 1, 2, 3, summed over the three seeds.
Each seed has ~470 scored ads: the standard stream with its deals, normal ads and every standard trap type, plus 12 % hidden gems
(7 kinds) and 5 % gem-shaped traps. The scout's model is simulated as JSON text going through the real engine:

* **oracle**: correct readings;
* **noisy**: 15 % wrong model, 20 % missed parts, 8 % broken JSON, 3 % shifted indexes, ±2 interest;
* **weak**: a 3–4B on a 4-core CPU. 35 % not identified, 12 % hallucinated models/parts, 20 % broken JSON (truncated,
  fenced, prose), 6 % shifted indexes, ±3 interest, ~10 s/ad, so most ads overflow to the script path and the
  "candidates" mode kicks in.

Stage C (photos) is an oracle-like vision fake of the same quality level for both pipelines. "Precision" counts a VB
"buy" whose offer is a real deal as correct, like the standard benchmark's «buy + торг».

| model quality | scout | precision «buy» | recall «buy» | gems bought | gems in feed (buy+maybe) | trap buys | buys found by scout | ads read / not reached | broken answers | vision calls |
|---|---|---|---|---|---|---|---|---|---|---|
| oracle | off (script only) | 100.0 % (267/267) | 72.8 % | 74/143 | 85 % | 0 | 0 | 0 / 0 | 0 | 484 |
| oracle | on, gpu_7b | 100.0 % (313/313) | 85.3 % | 115/143 | 96 % | 0 | 111 | 1401 / 3 | 0 | 475 |
| noisy | off (script only) | 100.0 % (248/248) | 67.8 % | 74/143 | 85 % | 0 | 0 | 0 / 0 | 0 | 484 |
| noisy | on, gpu_7b | 100.0 % (275/275) | 75.1 % | 101/143 | 92 % | 0 | 76 | 1402 / 2 | 24 | 474 |
| weak | off (script only) | 100.0 % (248/248) | 67.8 % | 74/143 | 85 % | 0 | 0 | 0 / 0 | 0 | 484 |
| weak | on, cpu_3b | 100.0 % (260/260) | 71.0 % | 90/143 | 90 % | 0 | 45 | 945 / 1211 | 91 | 481 |
| gem type | script only | scout oracle | scout noisy | scout weak (CPU 3B) |
|---|---|---|---|---|
| typo | 20/21 buy, +1 maybe | 20/21 buy, +1 maybe | 20/21 buy, +1 maybe | 20/21 buy, +1 maybe |
| vague_text | 20/21 buy, +1 maybe | 19/21 buy, +2 maybe | 20/21 buy, +1 maybe | 18/21 buy, +3 maybe |
| unknown_model | 19/21 buy, +2 maybe | 19/21 buy, +2 maybe | 18/21 buy, +3 maybe | 19/21 buy, +2 maybe |
| pc_gpu | 0/21 buy, +0 maybe | 16/21 buy, +1 maybe | 8/21 buy, +3 maybe | 6/21 buy, +1 maybe |
| konvolut | 0/21 buy, +21 maybe | 16/21 buy, +5 maybe | 12/21 buy, +9 maybe | 6/21 buy, +15 maybe |
| bundle | 0/21 buy, +21 maybe | 9/21 buy, +10 maybe | 8/21 buy, +11 maybe | 5/21 buy, +15 maybe |
| wrong_category | 15/18 buy, +3 maybe | 16/18 buy, +2 maybe | 15/18 buy, +3 maybe | 16/18 buy, +2 maybe |

Reading it:

* **Precision stays 100 % and no trap is bought in any configuration**, including the weak model that breaks JSON,
  shifts indexes and invents parts. The two trap leaks found while building this (a «Switch nur Tablet» re-promoted
  as a full console, and a GTX 1060 PC priced as an RTX 3070 by the vision model's search phrase) led to two rules:
  the scout may only overrule the `complete_pc`/`laptop` veto, and a PC/bundle is never priced by the vision
  phrase.
* The scout's gain is where the script is structurally blind: **PCs, lots and bundles** (0 «buy» without it; lots
  and bundles only reached «maybe» through the vision model's own price guess). Typos, vague titles, unknown models
  and wrong categories were already mostly found by script + vision in this benchmark. Its vision budget (30 per
  pass) was enough at n = 400. With the paid-step budgets under pressure (big category scans), the scout's ranking
  matters more.
* Even the weak CPU model helps. It reads only ~45 % of the ads and the rest take the script path.
* The standard benchmark (`python -m ebeyparser.benchmark --seed 1 --n 600`, scout off) is unchanged: precision
  100 %, recall 92.6 %, 0 trap buys, invariant ✔.

### 14.2 Real-LLM smoke test

(filled in below)

## 15. Open issues / next steps

* **Deep read**: a gem whose telling detail is past the 150-character snippet can't be seen by stage A. A budgeted
  "detail + second read" for high-priority vague PC/lot ads would catch it.
* **CPU/RAM prices**: PCs are valued by their GPU (+ any priced CPU). A small reference table or dedicated history
  for CPUs would make PC values less conservative.
* **Concurrency**: triage currently runs inline, within `pass_share` of the interval. Running it while the scraper
  waits on its polite request delays would hide most of its time.
* **Too-cheap gems**: a real gem at < 40 % of market is capped at «maybe / проверь, не развод» by the existing
  anti-scam rule. The scout's `hidden` tags could relax this for clearly clueless sellers with pickup, but only after
  real-world evidence.
