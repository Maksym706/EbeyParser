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
  │       scout identified a different product); interest only breaks ties; blocked readings rank last
  │
  ├─ C  PAID: ad page → re-ground on the FULL text (drops «ohne Grafikkarte» cases) →
  │     comparables via the scout's key/phrase or for ≤ 2 unpriced bundle parts →
  │     VISION check (existing _ask_ai; PC/bundle item types are no veto when the scout expects them;
  │     no comparables shown for part-priced bundles; product disagreement caps at "maybe")
  │
  └─ D  DECISION: existing evaluate() (fees, profit, ROI, every hard rule) → tier (super/deal/…) →
        alert: «🔥 Супер-находка» at once past the hourly cap; others as before; «Топ за день» digest.

end of pass: SCOUT RESCUE — scout time left over → read recent dismissed ads it didn't reach, re-plan
             stored readings (history grows) → promote by data → paid stage.
start of pass: VISION QUEUE — would-be deals whose photos the offline PC couldn't check.
```

### Two AI endpoints

The home server runs the app 24/7. It is weak: CPU only, 4–16 GB RAM, maybe ARM. The gaming PC with a GPU is on only
sometimes (LAN/Tailscale).

| role | runs on | model | config |
|---|---|---|---|
| **scout** (stage A) | the always-on server's CPU | 2–4B text model: Qwen3.5 2B (T0) / 4B (T1), see the catalog; nothing under 2B | `ai.scout.*` (own `base_url`, `model`) |
| **vision** (stage C) + second look | the PC when it's on | 7B+ vision model | `ai.*` |

An empty `ai.scout.base_url`/`model` means the scout uses the `ai` endpoint.
**When the vision endpoint is offline**, a candidate the math calls a deal is stored as `would_buy` with
`ai_checked=False` and put into `vision_queue`. Each pass starts by draining the queue. If the model is back, it checks
the photos and the deal alerts normally (`deal_updated` event). After `ai.vision_wait_minutes` (default 45), the deal
goes out marked «⚠ ФОТО НЕ ПРОВЕРЕНЫ ИИ — проверь сам» (the existing `notifications.unchecked_deals` rule). Scraping
never waits for it.

## 3. The triage prompt (small-model friendly)

`ai/prompts_triage.py`. The design targets 2–4B models on a CPU; bigger ones only get better. **Models under 2B are
refused** (`triage.too_small_for_triage`, e.g. Qwen3.5 0.8B, Qwen2.5 1.5B): in the model research the 0.8B renumbered
ads and copied their text (kind 20 %). The monitor then leaves the scout off (the user's model stays as configured),
the status says «Модель … слишком маленькая для разведчика», and `POST /ai/scout/test` suggests a catalog model.

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
  * **`sale` is the explicit default kind** (the parser maps it to the internal `single`), and every other kind lists
    its German trigger words: Suche / Suche nach / Kaufe → wanted, Tausche / Tausch → swap, defekt / für Bastler /
    iCloud gesperrt → defect, Ersatzteil → part, nur OVP / nur Karton → box, Paket / Set → bundle, Konvolut /
    Sammlung / Nachlass → lot;
  * the Russian reason is **at most 8 words, in the model's own words, never copied German text**. The parser drops a
    reason without a single Cyrillic letter (the UI then builds its own line) and cuts it at 12 words;
  * model numbers and sizes are **copied verbatim**;
  * **thinking is off** for the scout's calls (`make_scout_llm`: `chat_template_kwargs.enable_thinking=false`,
    Ollama `think=false`);
  * **batch size by model size** (`triage.batch_for_model`, from the model id or the catalog): ~2B starts at 5 and
    stays ≤ 5 (the 2B read kinds at 80 % in batches of 5 vs 63 % in 10); 3B 8 (≤ 10); 4B+ 10 (≤ 16); unknown 8
    (≤ 16). `ai.scout.batch_size` / `max_batch` = 0 means "by the model"; a set value wins.

Keys: `k` kind ∈ sale·bundle·pc·lot·part·acc·box·wanted·swap·service·defect·other; `p` "Brand Model Variant
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

**Adaptive batch** (start and maximum by model size, see above; minimum 2): halves when < 70 % of a batch parses,
grows by 2 after 3 full successes. It is
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

The T0 row is the model research's measurement on this sandbox (`AI_MODELS.md` §4: **2B 6 s/ad, 4B 15.5 s/ad**);
the T1 row (**2B ~2.5 s/ad, 4B ~6 s/ad**) and the GPU rows are extrapolated from memory bandwidth. The same numbers
are `model_catalog.MODELS[*].sec_per_ad`, and the status line shows them («Ожидается ≈ 6.0 с на объявление, до 300
объявлений в час (пока не измерено)») until the scout has measured its own speed. Quality on the research's test set:
Qwen3.5 4B read product 97 %, kind 87 %, scam 100 %, JSON 100 %; the 2B at batch 5 read product 72 %, kind 80 %,
scam 88 %, JSON 100 %. Their `interest` barely separates gems from junk (AUC 0.43 for 2B, 0.63 for 4B), so it
decides nothing (§6). Output tokens dominate on CPU: the minified, one-letter-key format is worth ~2–3×.

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
  defect, other, acc, part`, it has no risk tag in `scam, locked, fake, rent, defect, missing, reserved`, and **the
  code's own red flags** (`scout.code_red_flags`, `pricing/text.py`) find nothing on the ad text: no severe flag
  (Vorkasse, PayPal Freunde, Western Union, «Sicher bezahlen» + link/e-mail/messenger, wanted, swap, defect, lock, …),
  no WhatsApp / Telegram / e-mail contact, and on Kleinanzeigen no "nur Versand / keine Abholung". The 2B missed exactly
  these scams in the model research, so the code backs it up;
* the product, or at least one bundle part, is grounded;
* the dismissal was one of two things, and nothing else:
  * a **kind veto the scout may overrule** (`SCOUT_OVERRULES`): `complete_pc` only when the scout read the ad as a
    PC/lot/bundle priced by its parts, `laptop` only as an identified single product. `part` (missing components,
    «Switch nur Tablet»), `accessory`, `defect`, `box_only`, `wanted`, `swap` and `service` are never overruled;
  * an **early skip** where the scout saw a different product (`_scout_sees_other`).

  Stop words, include keywords, price ranges, `min_listing_price` and severe flags are never overruled either;
* **data decides, not the model's interest**: the math on the scout's history price says "possible deal"
  (`evaluate()` without AI is not a no-deal), or there is something concrete for the paid stage to price: an identified
  single product the history doesn't know (a comparables lookup with the scout's key/phrase follows), or a bundle's
  model-numbered parts without a price yet. A bundle with nothing priceable is not promoted, whatever the interest.
  `min_interest` [0] is only a weak veto for users of bigger models; at 0 the interest never filters.

The paid stage re-grounds on the full ad text. If the reason is gone («ohne Grafikkarte» further down), the script's
original decision is stored.

**The scout never drops an ad on its own.** A script candidate whose scout price says "no deal" (a weak model may have
read a cheaper variant) is ranked after the unknown ones but keeps the script's own path. Ranking in the paid stage
is by data too: cost / market when priced; unpriced ones rank together and the interest only breaks ties
(±0.01 around the unknown rank); blocked readings rank last. **A PC/bundle/lot is never priced by the vision model's search phrase**, which names one
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
| misses a defect/scam/lock (the 2B missed WhatsApp / e-mail / «Sicher bezahlen» scams) | the code's red flags block the promotion (`code_red_flags`); the script's red flags on every evaluated ad (new: Telegram, e-mail address or "schreib mir eine E-Mail", «Sicher bezahlen» + link/e-mail/messenger, Zahlungslink); e-mail/Telegram contact counts as bait when far too cheap; the vision veto |
| broken JSON / wrong index | parser; grounding rejects a shifted item |
| over-values a bundle | lower-bound valuation, pc/bundle discount, thin-data caps |
| misses «nur Tablet» / «ohne Akku» (missing parts) and names the full product | the `part` kind veto is never overruled |
| an unpriced PC that the vision model prices by one part | no vision-phrase pricing for PC/bundle/lot plans |
| under-claims a variant (cheaper model) → "no deal" | the scout never drops an ad; the script path still runs |
| a weak model's interest score (near random for a 2B) | nothing depends on it: promotion needs a grounded product and price data; interest only breaks ties |

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
| `batch_size` [0], `min_batch` [2], `max_batch` [0] | adaptive batch; 0 = by the model's size (5/5 for ~2B, 10/16 for 4B+) |
| `max_per_hour` [600] | hard cap of ads read per hour |
| `pass_share` [0.5] | share of the check interval the scout may use per pass |
| `min_interest` [0] | a weak veto only: below it the scout doesn't promote; 0 = interest never filters (§6) |
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
* **`GET /api/v1/monitor`** → `scout`: `{enabled, state (off|idle|ok|behind|down|too_small), text_ru («Успевает
  смотреть 90 из 120 новых объявлений в час»), speed_ru («≈ 5.0 с на объявление, до 360 объявлений в час»; before the
  first measurement «Ожидается ≈ 6.0 с …, до 300 … (пока не измерено)» from the catalog), speed_expected, too_small,
  mode, mode_setting, mode_ru, provider, base_url, model, own_endpoint, seen_last_hour, read_last_hour,
  overflow_last_hour, failed_last_hour, sec_per_ad, capacity_per_hour, batch_size, vision_queue {waiting,
  max_wait_minutes, text_ru}}`.
* **`GET /api/v1/health`**: the same `scout` block. The banner warns when the scout is down, its model is too small,
  or deals wait for the photo check.
* **`GET/PATCH /api/v1/settings`**: `ai.scout.*`, `ai.vision_wait_minutes`, `notifications.super_deals.*`,
  `notifications.daily_top.*`.
* **`POST /api/v1/ai/scout/test`**: runs the triage on 4 built-in sample ads (hidden RTX 3070 in an old PC, a typo, a
  wanted ad, a pram) → `{ok, seconds, sec_per_ad, per_hour, answered, hidden_gpu, typo_fixed, wanted_seen, items[],
  message_ru, error_ru, suggested_model, too_small, recommended {key, name, ollama, lmstudio, llamacpp}}`. It picks
  the catalog's best installed triage model when none is set. A model under 2B is not run: `too_small=true` and a
  plain-Russian message naming an installed catalog model or the one to download (LM Studio name / Ollama name).
  `save=true` stores the endpoint and switches the scout on.
* **`GET /api/v1/ai/recommend`** (`?ram_gb=&vram_gb=&arm=&role=server|pc&detect=`): «Выбери своё железо → рекомендую
  модель» from `model_catalog.recommend`. Without parameters it uses this machine's hardware (`ai/hardware.py`:
  cores, RAM, arch, NVIDIA GPU via nvidia-smi if present). Returns `host`, `input`, `tier`, `summary_ru`, `picks`
  {triage, vision, embeddings, second_opinion} (ids per runtime, quant, RAM/VRAM, label/desc, `speed_ru`, `get_ru`
  per runtime), `notes_ru`, `installed_match` (per found LM Studio / Ollama server: which installed models already fit,
  via `best_installed`) and `setup_ru` per runtime. No shell commands in any text.
* **Events**: `deal_found` / `deal_updated` (from the monitor) keep their old keys and add `found_by`, `tier`,
  `listing` (JSON), `evaluation` (JSON) and `card`.

Frontend (for the SPA team, `ebeyparser/web/app/**`):

1. **«Нашла нейросеть» badge** on deal cards and the deal page when `card.found_by == "ai_scout"`, tooltip = `card.scout_reason`.
2. **«🔥 Супер-находка»** style (accent border/ribbon) when `card.tier == "super"`, and a feed filter «Только супер».
3. **Settings → Нейросеть**: a «Разведчик» block with an on/off switch; a mode segmented control
   «Авто / Все объявления / Только непонятные» (`ai.scout.mode`); an endpoint (address + model, default «как у
   нейросети для фото»); a «Проверить разведчика» button (`POST /ai/scout/test`, showing the 4 sample readings and
   «≈ N объявлений в час»); the live line `scout.text_ru` + `scout.speed_ru`; an advanced section (batch, cap/hour,
   share of interval, min interest); «Ждать проверку фото, мин» (`ai.vision_wait_minutes`). When the scout test
   says `too_small`, show its `message_ru` and offer `recommended`.
6. **«Выбери своё железо → рекомендую модель»** (onboarding and Settings → Нейросеть) from `GET /ai/recommend`: the
   detected machine as the default, a server/PC switch, per task a card (name, label_ru, desc_ru, speed_ru, the
   runtime's id with a copy button, get_ru), "already installed" from `installed_match`, `setup_ru` per runtime.
4. **Состояние**: a «Разведчик» tile with `scout.text_ru`, `speed_ru`, `mode_ru`, and «не успел N» (`overflow_last_hour`);
   a «Ждут проверки фото: N» line (`scout.vision_queue.text_ru`).
5. **Settings → Уведомления**: «Супер-находки сразу» (enabled + min profit/ROI) and «Топ за день» (enabled, hour, per search).

## 13. Monitor integration (what changed where)

* `__init__(…, scout=TriageEngine)` for tests/benchmarks; `_ensure_components` builds the engine from `ai.scout`
  (`_scout_llm_settings` falls back to `ai`; batch size by model size), but not for a model under 2B
  (`_scout_too_small`: logged once, status `too_small`).
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
* `scout_status()` for the API; `_scout_expected_speed` gives the catalog's seconds per ad for this machine's tier
  (an endpoint elsewhere counts as T0) until the scout has measured its own.

## 14. Evidence

**Benchmark** (`python -m ebeyparser.benchmark_gems --seed 1 --n 400`). This is the standard synthetic Berlin market
(deals, normal ads, all traps) plus 12 % hidden gems and 5 % gem-shaped traps. The real Monitor runs with the scout
off and on. The scout's model is simulated as JSON text through the real engine, at three qualities, with simulated
GPU/CPU speed. See §14.1.

**Real LLM smoke test**: Qwen3.5-2B Q4_K_M on this 4-core sandbox CPU via llama.cpp `llama-server`
(OpenAI-compatible) on 16 of 40 realistic German ads. See §14.2.

### 14.1 Benchmark results

`python -m ebeyparser.benchmark_gems --seed S --n 400` for S = 1, 2, 3, summed (seeds [1, 2, 3]).
Each seed has ~470 scored ads: the standard stream with its deals, normal ads and 173-ish traps, plus 12 % hidden gems
(7 kinds) and 5 % gem-shaped traps. The scout's model is simulated as JSON text going through the real engine:

* **oracle**: correct readings;
* **noisy**: 15 % wrong model, 20 % missed parts, 8 % broken JSON, 3 % shifted indexes, ±2 interest;
* **weak**: a small model on a 4-core CPU. 35 % not identified, 12 % hallucinated models/parts, 20 % broken JSON
  (truncated, fenced, prose), 6 % shifted indexes, **interest pure noise** (0–10 at random, as the model research
  measured for Qwen3.5 2B: AUC 0.43), ~10 s/ad, so most ads overflow to the script path and the "candidates" mode
  kicks in.

Stage C (photos) is an oracle-like vision fake of the same quality level for both pipelines. "Precision" counts a VB
"buy" whose offer is a real deal as correct, like the standard benchmark's «buy + торг».

| model quality | scout | precision «buy» | recall «buy» | gems bought | gems in feed (buy+maybe) | trap buys | buys found by scout | ads read / not reached | broken answers | vision calls |
|---|---|---|---|---|---|---|---|---|---|---|
| oracle | off (script only) | 100.0 % (267/267) | 72.8 % | 74/143 | 85 % | 0 | 0 | 0 / 0 | 0 | 484 |
| oracle | on, gpu_7b | 100.0 % (312/312) | 85.0 % | 115/143 | 96 % | 0 | 111 | 1401 / 3 | 0 | 483 |
| noisy | off (script only) | 100.0 % (248/248) | 67.8 % | 74/143 | 85 % | 0 | 0 | 0 / 0 | 0 | 484 |
| noisy | on, gpu_7b | 100.0 % (280/280) | 76.5 % | 104/143 | 92 % | 0 | 81 | 1402 / 2 | 28 | 484 |
| weak | off (script only) | 100.0 % (248/248) | 67.8 % | 74/143 | 85 % | 0 | 0 | 0 / 0 | 0 | 484 |
| weak | on, cpu_3b | 100.0 % (253/253) | 69.1 % | 84/143 | 94 % | 0 | 44 | 924 / 1176 | 74 | 491 |
| gem type | script only | scout oracle | scout noisy | scout weak (CPU 3B) |
|---|---|---|---|---|
| typo | 20/21 buy, +1 maybe | 20/21 buy, +1 maybe | 20/21 buy, +1 maybe | 19/21 buy, +2 maybe |
| vague_text | 20/21 buy, +1 maybe | 19/21 buy, +2 maybe | 20/21 buy, +1 maybe | 18/21 buy, +3 maybe |
| unknown_model | 19/21 buy, +2 maybe | 19/21 buy, +2 maybe | 19/21 buy, +2 maybe | 19/21 buy, +2 maybe |
| pc_gpu | 0/21 buy, +0 maybe | 16/21 buy, +1 maybe | 9/21 buy, +3 maybe | 9/21 buy, +5 maybe |
| konvolut | 0/21 buy, +21 maybe | 16/21 buy, +5 maybe | 13/21 buy, +8 maybe | 3/21 buy, +18 maybe |
| bundle | 0/21 buy, +21 maybe | 9/21 buy, +10 maybe | 8/21 buy, +11 maybe | 4/21 buy, +16 maybe |
| wrong_category | 15/18 buy, +3 maybe | 16/18 buy, +2 maybe | 15/18 buy, +3 maybe | 12/18 buy, +6 maybe |

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
* Even the weak CPU model helps, although its interest score is random: promotion is decided by grounded products
  and price data only (§6), and its reading ranks candidates. It reads only ~45 % of the ads and the rest take the
  script path. With a weak model whose interest *does* carry signal (the earlier simulation), the same rules give 87
  gems / recall 70.2 %; the previous interest-driven rules gave 90 / 71.0 % there, the difference being lots and
  bundles promoted on a high score alone (now: only with something priceable).
* The code's red flags block promotions the model didn't flag (WhatsApp / Telegram / e-mail, «Sicher bezahlen» links,
  Vorkasse, "nur Versand"): no gem-shaped scam got through in any configuration.
* The standard benchmark (`python -m ebeyparser.benchmark --seed 1 --n 600`, scout off) is unchanged: precision
  100 %, recall 92.6 %, 0 trap buys, invariant ✔.

### 14.2 Real-LLM smoke test

**Setup.** Hugging Face is blocked from this sandbox (proxy 403). The llama.cpp release (b10830, CPU) came from GitHub
releases, and `qwen2.5-1.5b-instruct-q4_k_m.gguf` from a GitHub release mirror (size matches the official file). The
model research downloaded the Qwen3.5 GGUFs, and the smoke test used **Qwen3.5-2B Q4_K_M** (the catalog's T0 pick)
read-only. `llama-server` (OpenAI-compatible, private port) went through the **real** `VisionLLM` client
(`json_schema` response format, thinking off) and the **real** `TriageEngine` (batch 8). The ads were
`scratchpad/ai/triage_testset.py`: 40 realistic German Kleinanzeigen-style ads (16 gems, 12 traps, 12 normal), 16 of
them used here (every other one: 8 gems, 6 traps, 2 normal).

**The CPU was shared.** Another agent was benchmarking models on the same 4 vCPUs the whole time (load 5–8). With 4
threads, llama.cpp slowed to 0.39 tok/s from oversubscription, so this run used **1 thread**. Treat the speed as a
worst case. The clean-machine numbers are the model research's measurements (`AI_MODELS.md` §4: 2B ≈ 6 s/ad, 4B ≈
15.5 s/ad on this VM).

| metric | result |
|---|---|
| ads / calls | 16 / 4 (final batch size 4) |
| valid JSON items (matched by index) | **16/16** (100 %), script fallback 0 |
| kind correct | 69 % |
| product / model tokens correct | 94 % |
| gems identified **and grounded** | 7/8 |
| gems with interest ≥ 6 | 7/8 |
| traps with interest ≤ 4 | 5/6 |
| risky ads flagged (risk tag or wanted/defect/box/swap kind) | 1/4 |
| wall time, s/ad (1 contended thread) | 304.4 s, **19.02 s/ad** |

Per ad (K = kind ok, M = model ok, G = product grounded in the ad text):

| | title | kind | product | contents | s | risks | reason |
|---|---|---|---|---|---|---|---|
| KMG | Alter Rechner vom Dachboden | pc | Intel Core i5-9600K | RTX 3070, 16GB RAM | 9 |  | старый ПК, RTX 3070, i5 9600k |
| ··· | Iphne 13 128gb blau | acc | Apple iPad Air 5 64GB |  | 6 |  | iPad Air 5 с опечаткой в названии |
| KMG | Playstaion 5 mit Laufwerk | bundle | PlayStation 5 Disc |  | 0 |  | игра на PS5 |
| KMG | Grafikkarte von Nvidia | part | NVIDIA GeForce RTX 3070 Ti |  | 6 |  | GeForce RTX 3070 Ti |
| KMG | Kiste Technik aus Haushaltsauflösung | bundle | Sony WH-1000XM4 | GoPro Hero 11 Black, Ladekabel | 6 |  | Sony WH-1000XM4 с GoPro |
| ·MG | Spielzeug Drohne | acc | DJI Mini 3 |  | 6 |  | дрона DJI Mini 3 |
| ·MG | Tablet Apple | acc | Apple iPad Air 5 64GB |  | 6 |  | iPad Air 5 с опечаткой в названии |
| KMG | Gaming PC günstig | pc | Intel Core i7-8700K | GTX 1080 Ti 11GB, 32GB RAM, 1TB SSD | 9 |  | i7 8700K, GTX 1080 Ti, 32GB RAM |
| KM· | Gaming PC ohne Grafikkarte | pc | Gaming PC ohne Grafikkarte |  | 9 |  | PC без видеокарты, i5, 16GB RAM, 650W |
| KM· | Suche alten PC mit Grafikkarte | wanted | PC mit RTX 3080 |  | 0 |  | старый ПК с видеокартой |
| KMG | iPhone 14 Pro iCloud gesperrt | defect | Apple iPhone 14 Pro |  | 0 | locked | iCloud заблокирован |
| ·MG | Konvolut defekte Handys | bundle | iPhone 12 |  | 0 |  | конволут с поломанным экраном |
| ·MG | Airpods Pro 2 original | acc | AirPods Pro 2 |  | 0 |  | наушники с пробным периодом |
| KMG | Hülle für iPhone 13 | acc | Silikon Hülle iPhone 13 |  | 3 |  | чехол для iPhone 13 из силикона |
| KMG | iPhone 13 128GB Mitternacht | single | Apple iPhone 13 128GB |  | 6 |  | iPhone 13 с опечаткой в названии |
| KMG | Nintendo Switch Lite Türkis | single | Nintendo Switch Lite |  | 0 |  | Switch Lite с зарядкой и чехлом |

Reading it:

* **The format holds on a 2B model.** All 16 answers were valid JSON matched by index, with no script fallback. The
  first call (8 ads, 588 output tokens, 168 s) went over the time cap, so the engine cut the batch to 4 and re-asked
  for the one missing item. Output was 1104 tokens for 16 ads (**~69 tokens/ad**). llama.cpp reuses the cached system
  prompt, so after the first call the prompts were only 137–357 tokens.
* **What it finds is the script's blind spot.** Both PCs were read as `pc` with the GPU in the parts (RTX 3070 in
  «Alter Rechner vom Dachboden», GTX 1080 Ti in «Gaming PC günstig»). It also found:
  * the unnamed «Grafikkarte von Nvidia» (RTX 3070 Ti);
  * the «Kiste Technik» contents (Sony WH-1000XM4 + GoPro Hero 11);
  * the «Playstaion» typo.
* **Its mistakes are the ones grounding is for.** The 2B model mixed up two neighbouring ads in one batch: «Iphne 13
  128gb blau» came back as "Apple iPad Air 5 64GB", the next ad's product, and «Tablet Apple» got the iPhone ad's
  reason. The product is not in the iPhone ad's text, so it is ungrounded, and that ad simply takes the script path.
  «Gaming PC ohne Grafikkarte» got interest 9. Still:
  * its product is negated («ohne»), so it is ungrounded;
  * it has no parts, so it is not usable.
  
  So the scout does not promote it; the script and the photo check decide as before.
* **Kinds and risk tags are weak at 2B** (kind 69 %; only the iCloud lock was tagged). The WhatsApp-only €40 AirPods
  and the broken-phones lot were not tagged, but got interest 0, so they rank last. The existing scam/defect rules
  still apply to every ad, because the scout never overrules them. This matches the model research: 4B is much
  better on kind (87 %) and scam (100 %), and is the pick when the home server can afford ~15 s/ad.
* **Speed** here, 19 s/ad on one contended thread, is a worst case. On a free 4-core CPU the research measured ≈ 6
  s/ad for 2B, i.e. ~600 ads/hour, which is what `max_per_hour` 600 assumes.

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
* **Output length**: ~69 output tokens/ad in the smoke test, against 46–55 in the model research's shorter schema.
  On a CPU, output tokens are the cost. Dropping `q` (the scout's search phrase, often the product again) or
  shortening `r` could buy ~20–30 % more ads per hour. Measure before cutting: `q` feeds the comparables lookup.
* **Batch cross-talk** on 2B models (one ad's product given to its neighbour) is caught by grounding. ~2B models now
  run at batch ≤ 5 by default (the smoke test above used 8); if it still shows up often, `ai.scout.max_batch: 4`.
