# Free cloud AI: NVIDIA Nemotron on OpenRouter, the NVIDIA API catalog and OmniRoute

Status: implemented on **2026-09-30**. The code is in `ebeyparser/ai/cloud.py` (presets, key, limiter,
budget, parsing, thinking switch, privacy), `ebeyparser/ai/client.py` (`VisionLLM` on a cloud
endpoint), `ebeyparser/web/api/routes_cloud.py` (API), `ebeyparser/web/app/js/setup/cloud.js`
(UI). Tests: `tests/test_cloud_ai.py`, `tests/test_spa_cloud.py`, the cloud profiles in
`tests/test_benchmark_gems.py`.

Related: `AI_MODELS.md` (local models), `AI_SCOUT.md` (the triage pipeline these models feed).

**Rule zero still holds: models never produce prices.** A cloud model reads ads and looks at
photos like the local one does. Every euro comes from our own price history.

---

## 1. Why

The user has a weak PC. A local 2B model reads ads slowly and misreads variants, and photo checks
on a CPU take about 40 s each (`AI_MODELS.md` §4). Free cloud endpoints run much bigger models,
such as Nemotron 3 Super 120B-A12B for text and Nemotron Nano 12B VL for photos, in a few seconds.
Kleinanzeigen and eBay ads are public data. The catch is the quota, the trial terms and the
privacy of what is sent. The design below deals with all three.

## 2. Research (as of September 2026)

The development sandbox cannot reach these hosts, so these facts come from the research brief of
September 2026. They were not checked from here. Every number that matters can be checked on the
user's machine with «Проверить» (§8), which reads it from the live service. The limits are never
hard-coded without that check: `/key`, the rate-limit headers and a daily 429 correct the
presets at run time (§4.3).

| | OpenRouter | NVIDIA API catalog | OmniRoute |
|---|---|---|---|
| What | A hosted router in front of many providers; free variants carry `:free` | NVIDIA's own endpoints (build.nvidia.com) | A self-hosted MIT gateway in front of many free providers, with quota-aware auto-fallback |
| Address | `https://openrouter.ai/api/v1` | `https://integrate.api.nvidia.com/v1` | `http://localhost:20128/v1` |
| Key | `sk-or-v1-…` from openrouter.ai/keys | `nvapi-…` from build.nvidia.com (free NVIDIA Developer account) | optional |
| Per minute | 20 requests on free models | ~40 per account, shared by all models | depends on the providers behind it |
| Per day | **50**. **1000** once $10 of credits was ever bought (one purchase; free models do not spend credits). Resets 00:00 UTC | no daily cap (the credit model ended) | depends |
| Text models | `nvidia/nemotron-3.5-lightning:free` (**the user's choice, the default pick**), `nvidia/nemotron-3-super-120b-a12b:free`, `nvidia/nemotron-3-ultra-550b-a55b:free` | the same families without `:free` | whatever is connected |
| Vision models | `nvidia/nemotron-nano-12b-v2-vl:free` (JPEG/PNG ≤ 1024², 128k context), `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` (reasoning toggle `reasoning.enabled`) | `nvidia/nemotron-nano-12b-v2-vl`, Nano Omni | — |
| Limits API | `GET /api/v1/key`: `is_free_tier`, `limit_remaining`, `rate_limit`, … | none (the rate-limit headers only) | — |
| Model list | `GET /api/v1/models`: `pricing`, `architecture.input_modalities` (free and vision can be filtered live) | `GET /v1/models` (ids only) | `GET /v1/models` |
| Reasoning off | `reasoning: {enabled: false}` | Nemotron: a `detailed thinking off` (Llama-Nemotron v1) or `/no_think` system line; `chat_template_kwargs.enable_thinking` | the upstream decides; only the soft switch is sent |
| Terms, risks | Upstream 429s are common even within quota. Some free providers may log or train on prompts (an account setting) | "Trial / prototyping", no SLA | runs on the user's machine; the providers' terms apply |

Sources: openrouter.ai/docs (API reference: limits, `/key`, `/models`, the reasoning parameter),
openrouter.ai/settings/privacy, build.nvidia.com (model cards of the Nemotron family, API keys),
github.com/diegosouzapw/OmniRoute. Research date: September 2026.

## 3. Configuration (backward compatible: the defaults keep the local behaviour)

```yaml
ai:
  cloud: openrouter          # "" (default) = a local server; openrouter | nvidia | omniroute | custom
  provider: openai           # every cloud endpoint is OpenAI-compatible
  base_url: https://openrouter.ai/api/v1
  model: nvidia/nemotron-nano-12b-v2-vl:free      # the photo check
  rpm: 0                     # 0 = auto: the preset, then /key and the rate-limit headers
  daily_limit: 0             # 0 = auto: OpenRouter 50 (1000 after $10), NVIDIA/OmniRoute none
  thinking: auto             # auto | off | on: auto = off for the photo check, the scout and the planner
  fallback:                  # «Облако + компьютер про запас»
    enabled: true
    provider: openai
    base_url: http://localhost:1234/v1
    model: qwen/qwen3.5-9b
  cloud_send_ebay: false     # eBay ads never go to a cloud endpoint (§7)
  scout:
    enabled: true
    base_url: ""             # "" = the ai endpoint, its cloud kind, limits and key included
    model: nvidia/nemotron-3-super-120b-a12b:free
    fallback: {enabled: false, model: ""}   # "" = ai.fallback's server (model: this or ai.fallback.model)
```

Design choices:

* **One `cloud` kind** on `LLMSettings`, so `ai`, `ai.scout` and `ai.second_opinion` all get it.
  It is also detected from the address (`openrouter.ai`, `*.api.nvidia.com`, port 20128), so an
  address pasted by hand works too.
* **One fallback per role** (`ai.fallback`, `ai.scout.fallback`) instead of an ordered endpoint
  list. The UI has exactly three choices, and a list would need a list editor for a non-technical
  user. The scout inherits `ai.fallback` like it inherits the `ai` endpoint.
* **Keys only in `.env`** (`OPENROUTER_API_KEY`, `NVIDIA_API_KEY`, `OMNIROUTE_API_KEY`,
  `CLOUD_API_KEY`), written by the settings API like the Telegram token (`PUT /secrets`,
  `ApiContext.write_secrets`). `config.yaml` holds no key and no placeholder: the client takes the
  provider's variable itself (`cloud.resolve_api_key`), so keys of several providers can live side
  by side and switching providers needs no key edits. `GET /settings` shows them only as
  `{set, masked: "…abcd"}`. Error texts pass through `redact_key`, and the key is never logged.
* **Switching back** («На моём компьютере») restores the local model remembered in `ai.fallback`
  when the cloud was switched on.
* **Model picks** come from the live `/models` list. For reading ads on OpenRouter the order is
  Nemotron 3.5 Lightning (the user's choice), then 3 Super, then 3 Ultra. Each is tried by its id,
  then its id without `:free`, then by name tokens, so renamed ids still match. For photos the
  order is Nemotron Nano 12B VL, then Nano Omni. A model the list no longer offers gets a plain
  «выбери другую».

## 4. The client and the limiter

### 4.1 One quota per account

`QuotaLimiter` is keyed by `sha256(base_url | key)[:16]`, so the key never appears in the id.
The scout and the photo check on one account share one limiter (`cloud.registry()`). The
monitor attaches the database, so the day's counters survive restarts (`kv_state`,
`cloud:quota:<id>`).

* **Per-minute limit:** a token bucket with `rpm` tokens (a full minute's burst) that refills at
  `rpm/60` per second.
* **Daily counter:** counted per UTC day and per purpose (`vision`, `triage`, `test`), reset at
  00:00 UTC. Every request counts, including failed ones: the provider counts them too.
* **429:** `Retry-After` (seconds or an HTTP date) or `X-RateLimit-Reset` (epoch milliseconds,
  epoch seconds or seconds from now) is honoured. On top of that comes an exponential backoff
  (2 s, 4 s, 8 s …, at most 120 s) with ±25 % jitter. A 429 whose body mentions a per-day limit, or
  whose reset is more than an hour away, blocks until the reset. If that happens below the assumed
  daily limit, the observed count is learned as the real limit (for example, an account that paid
  less than $10 has 50, not 1000).
* **`X-RateLimit-Remaining: 0`** with a reset in the future pauses until that reset.
* **Circuit breaker:** after 3 consecutive 429s, 5xx responses or network errors, the endpoint
  pauses for 60 s × 2^(n−3), at most 15 min. Any success resets the count.

### 4.2 Never waiting for quota

A call waits **at most 6 s**: one RPM token at 20/min is 3 s, and a short 429 is retried once. A
longer wait raises `CloudLimited(LLMError)` at once, without a request. The caller then takes:

* the local stand-in, if one is configured (`CloudRouter`: the cloud first, the local model while
  the cloud is limited, down or not allowed);
* otherwise the existing "AI down" paths. The scout's ads go the script way, and nothing is
  reported as down, because the quota is not a broken server (the «Облако» tile shows it). Photo
  checks give `ai_checked=False`: the would-be deal waits in `vision_queue` up to
  `ai.vision_wait_minutes` and then goes out marked «фото не проверены». The alert is a quota
  message sent at most every 6 h ("⚠ Бесплатный лимит … исчерпан — продолжу завтра в 02:00"),
  not "AI down".

The monitor loop never sleeps on quota.

### 4.3 What goes over the wire

* OpenRouter gets the headers `HTTP-Referer: https://github.com/Maksym706/EbeyParser` and `X-Title: EbeyParser`
  (the app's name only).
* The format ladder json_schema → json_object → none is unchanged. On a cloud endpoint the step
  that worked is remembered, so no quota goes to requests the server rejects anyway.
* Images are sent as base64 `data:` URLs, downscaled to ≤ 1024 px JPEG (`prepare_images`), which
  fits Nemotron Nano VL's limit. No local URL is ever sent.
* `max_tokens` is at least 2600 for the scout (20 ads × 110 tokens) and 1500 for photos. Free
  tokens cost nothing but latency.
* Once a day the monitor reads OpenRouter's `GET /key`, which costs no quota:
  `is_free_tier` → 50 or 1000 a day.

### 4.4 Thinking off, per runtime

One switch, `thinking: auto | off | on` on `LLMSettings`, is mapped in
`cloud.thinking_controls`. `auto` means off for the photo check, the scout and the planner, and
the model's own default for the second opinion.

| Runtime | Off |
|---|---|
| llama.cpp / LM Studio / vLLM | `chat_template_kwargs: {enable_thinking: false}`; plus a `/no_think` system line for Qwen3 hybrid and Nemotron models |
| Ollama | `think: false` |
| OpenRouter | `reasoning: {enabled: false}` |
| NVIDIA catalog | `chat_template_kwargs.enable_thinking=false` and a `detailed thinking off` line (Llama-Nemotron v1) or `/no_think` (v1.5, Nano v2, Nemotron 3) |
| OmniRoute / custom | only the harmless soft switch; the upstream is unknown |

If a server rejects one of these fields (a 400 or 422 that names it, for example "Reasoning is
mandatory for this endpoint"), the field is dropped for good and the call is retried. Every answer
passes `strip_think`: `<think>…</think>` blocks and a reasoning prefix whose opening tag was in
the prompt are removed.

This also fixes the photo check on local llama-server, where Qwen3.5 thinks by default: all 700
tokens went into reasoning and each photo took 128–185 s, as the AI-QA agent measured.

## 5. The daily budget

`plan_budget(rpm, daily_limit, batch=20)`:

* **Photo checks come first.** `photos = min(L/2, max(5, round(0.2·L)))` requests a day are kept
  for them. The reserve shrinks linearly towards midnight, so an unused reserve goes to the scout
  late in the day.
* **The scout** gets `L − photos` calls at **16–20 ads per call** (`triage.batch_for_model`: known
  cloud models count as large; the local stand-in keeps its own small batches).
* **The scout is paced over the day.** It may have used `budget × (share of the UTC day) + burst`
  calls, where the burst is `max(3, 15 %)`. Beyond that it is refused, and those ads go the script
  way and are read again by the end-of-pass rescue later.
* **«Только непонятные».** Once it is ahead of the pace (`budget × day share + 5 %`) or has used
  75 % of its share, `mode: auto` reads only the ads the script is blind to: no model named, PCs,
  bundles, lots. It switches back after the reset.

| Account | Per minute | Per day | Photos a day | Scout calls | Ads a day | Text in the UI |
|---|---|---|---|---|---|---|
| OpenRouter free, < $10 ever bought | 20 | 50 | 10 | 40 | ~800 | «20 запросов в минуту · 50 в день (купи $10 кредитов один раз → 1000 в день) · … · хватит на ~800 объявлений в день» |
| OpenRouter free, ≥ $10 once | 20 | 1000 | 200 | 800 | ~16 000 | «… 1000 в день … хватит на ~16 000 объявлений в день» |
| NVIDIA catalog | ~40 | none | — | — | limited by the per-minute rate only (40 × 20 ads a minute) | «40 запросов в минуту · без дневного лимита … хватит на все объявления» |
| OmniRoute | its providers | its providers | — | — | — | set `rpm` / `daily_limit` if its providers need it |

For scale, a Berlin setup with category scans sees roughly 100–300 new ads an hour. At 50 a day
the scout reads about 800 ads a day with **all** calls used, and photo checks get 10. That is fine
for a few keyword searches but not for category scans. At 1000 a day or on NVIDIA it is enough.
The benchmark below shows the same.

## 6. Benchmark: the cloud profiles

`python -m ebeyparser.benchmark_gems --modes oracle,noisy --hardware cloud_free1000` (or
`cloud_free50`, `cloud_nvidia`) runs the real monitor on the synthetic Berlin market with hidden
gems and traps. The scout and the photo check go through the real `QuotaLimiter` on the
simulated clock: 20/min with 50 or 1000 a day (40/min with no daily cap for NVIDIA), random
upstream 429s (12 %, 5 % for NVIDIA) with a realistic Retry-After mix, and 3 s per cloud photo
check. eBay ads never go to the cloud (§7).

Seed 1 and seed 2, 400 stream ads each, about 2 simulated hours:

| Profile | Scout quality | Precision (s1 / s2) | Recall (s1 / s2) | Gems bought (s1 / s2) | Traps bought | Scout read / not reached (s1) | Cloud requests · 429s · photos refused (s1) |
|---|---|---|---|---|---|---|---|
| no scout, local GPU photos | — | 100 / 100 % | 74 / 73 % | 23/47 · 26/48 | 0 | — | — |
| local GPU 7B | oracle | 100 / 100 % | 84 / 83 % | 36/47 · 39/48 | 0 | 467 / 1 | — |
| local GPU 7B | noisy | 100 / 100 % | 79 / 78 % | 34/47 · 35/48 | 0 | 468 / 0 | — |
| weak local CPU 3B | weak | 100 / 100 % | 68 / 73 % | 27/47 · 29/48 | 0 | 305 / 396 | — |
| OpenRouter free, 50/day | oracle | 100 / 100 % | 19 / 13 % | 8/47 · 8/48 | 0 | 105 / 737 | 50 · 2 · 139 |
| OpenRouter free, 50/day | noisy | 100 / 100 % | 18 / 12 % | 8/47 · 7/48 | 0 | 107 / 735 | 50 · 2 · 136 |
| OpenRouter free, 1000/day | oracle | 100 / 100 % | 74 / 61 % | 30/47 · 21/48 | 0 | 383 / 358 | 182 · 13 · 65 |
| OpenRouter free, 1000/day | noisy | 100 / 100 % | 60 / 55 % | 25/47 · 19/48 | 0 | 374 / 349 | 177 · 13 · 69 |
| NVIDIA, 40/min, no daily cap | oracle | 100 / 100 % | 80 / 79 % | 36/47 · 37/48 | 0 | 436 / 29 | 227 · 8 · 20 |
| NVIDIA, 40/min, no daily cap | noisy | 100 / 100 % | 72 / 72 % | 34/47 · 33/48 | 0 | 436 / 37 | 239 · 8 · 20 |

What this shows:

* **Precision stays 100 % and no trap is ever bought in any profile, on both seeds.** A missing photo
  check caps a deal at «maybe» («фото не проверены»); it never promotes one.
* The cloud profile exposed a pricing hole in the scout itself, and it is fixed. A noisy scout had
  read «DeWalt DCD796 Schlagbohrschrauber **solo**» as the kit, and the kit's history price made
  the bare tool a «buy».
  * Now a scout reading that only *drops* variant words of the title never counts as another
    product (`Monitor._scout_sees_other`).
  * Its history price never replaces the ad's own when the title is more exact
    (`Monitor._scout_price_fits`).
  * The local profiles keep 100 % with it.
* **NVIDIA** is close to a local GPU. The gap is mostly the eBay rule: 20 eBay photo checks are
  never sent to the cloud.
* **OpenRouter at 1000/day** is on par with the script-only baseline or somewhat below it, and
  better than the weak local CPU model for gems on seed 1. What costs recall is the free endpoints'
  upstream 429s:
  * A long Retry-After stops a pass's scout, which leaves 290–360 ads not reached, and stops its
    photo checks early.
  * Those ads are read by later passes (the rescue).
  * The held deals are re-checked from `vision_queue`.
* **OpenRouter at 50/day** runs out within the first simulated hours on a category-scan workload:
  about 160 photo-check candidates in 2 hours against 50 requests a day, so recall of «покупать»
  collapses.
  * The would-be deals still reach the user, marked «фото не проверены».
  * The vague gems that the photo model would have identified are lost.
  * **Buy the $10 of credits once, use NVIDIA, or choose «Облако + компьютер про запас».**
* The simulation is pessimistic for the cloud. It compresses a pass into LLM time only, so a 30 s
  Retry-After blocks the rest of a simulated pass. A real pass lasts minutes (page delays), so
  such a pause usually ends before the next search.

Reproduce:
`python -m ebeyparser.benchmark_gems --seed 1 --n 400 --modes oracle,noisy --hardware cloud_free1000`.

## 7. Privacy and eBay

**Only the ad goes to the cloud:** title, description, price, item attributes, photos and the
comparable offers' titles and prices (all public).

* Stripped by `cloud.cloud_listing` for the photo check:
  * the seller's name, rating and seller attributes (Nutzertyp, Aktiv seit, Bewertung, …);
  * the postal code (the place is kept as the ad's own district or city);
  * the distance, which would tell where the user lives.
* Masked in every prompt to a cloud endpoint (`redact_contacts`, applied in the client):
  * phone numbers become `[Telefon]`;
  * e-mail addresses become `[E-Mail]`.

  The code's own scam rules (WhatsApp, e-mail contact, prepayment) still run locally on the full
  text.
* The scout's feedback hints for a cloud endpoint carry only titles (`feedback_hints(private=True)`).
  They leave out what the user paid or earned and the reasons they typed.
* Never sent: user tokens, the user's location or postal code, chats, notes.
* Audit:
  * `prompts_triage.build_user_prompt` sends title, price, category name, an eBay marker and the
    text snippet; it has no seller fields.
  * `prompts.build_user_prompt` has a seller line (`_seller_line`) and the full location; it gets
    the stripped listing on a cloud endpoint. Tests: `test_cloud_prompts_carry_the_ad_only`,
    `test_scout_prompt_to_the_cloud_has_contacts_masked`.

**eBay.** The eBay API License Agreement restricts sharing eBay content with third parties and
using it to train AI. Therefore `ai.cloud_send_ebay: false` is the default:

* eBay ads never go to a cloud endpoint. The client refuses under `cloud.local_only()`, set by the
  monitor for eBay listings.
* A local stand-in, if configured, reads and checks them.
* Otherwise:
  * the scout skips eBay searches, and they take the script path;
  * photo checks give «фото не проверены» right away, without waiting in the vision queue for a
    model that will never see them.

The UI says this in one plain line next to a switch («Отправлять объявления с eBay в облако»).

## 8. How to check on the user's machine (no shell commands)

1. **Настройки → Нейросеть → «Где работает нейросеть» → «Бесплатное облако».**
2. Choose **OpenRouter**, **NVIDIA** or **OmniRoute**. Then:
   1. click «Получить ключ»;
   2. paste the key;
   3. click **«Проверить»**.
3. «Проверить» (`POST /api/v1/ai/cloud/test`) does this from the user's machine:
   * checks the key (OpenRouter `/key`; the others answer on the first call);
   * lists the free models, with vision marked (OpenRouter: `/models` with pricing and
     modalities; NVIDIA/OmniRoute: `/models`) and the recommended picks preselected by name, so
     renamed ids still match;
   * reads the limits (`is_free_tier`, the X-RateLimit headers);
   * sends 5 German demo ads to the text model (latency, and whether it found the GPU in the old
     PC, the typo and the wanted ad);
   * sends one demo photo to the vision model.

   Expected line, for example: «Ключ работает · 20 запросов в минуту · 50 в день (купи $10
   кредитов один раз → 1000 в день) · ответ за 2,1 с · хватит на ~800 объявлений в день». A
   working key is saved to `.env` right away.
4. **«Сохранить»** (`PUT /api/v1/ai/cloud`) switches the photo check and the scout to the cloud.
   «Облако + компьютер про запас» also asks for the local model (LM Studio / Ollama detection).
5. **Состояние → «Облако»** shows:
   * today's requests and the limit, as a bar;
   * the next reset;
   * the per-minute limit;
   * the service's «подожди» (429) count;
   * whether the local stand-in is ready or working now;
   * the day's capacity.

   The same block is in `GET /api/v1/health` and `/monitor` (`cloud`).

## 9. Risks

| Risk | What we do |
|---|---|
| Quotas change (OpenRouter has changed its free limits before) | Nothing is trusted blindly: `/key` daily, the rate-limit headers, and a per-day 429 teaches the real limit. `rpm` / `daily_limit` can be set by hand |
| Model ids change or free variants disappear | The picks match the live list by name tokens (Nemotron + Super / VL). A missing model is a clear «модели нет — выбери другую» |
| Upstream 429s even within quota | One short retry, backoff with jitter, the circuit breaker. Ads go the script way or to the stand-in; deals wait in the vision queue |
| NVIDIA's "trial / prototyping" terms, no SLA | The service can disappear. The circuit breaker and the fallback keep the app working, and the local path is one click away |
| Some free providers log or train on prompts | Only public ad data is sent, with seller contacts masked. The UI links OpenRouter's privacy settings. eBay content never goes out |
| A cloud reasoning model ignores "thinking off" | `max_tokens` has room (1500 / 2600) and `<think>` is stripped. A rejected switch is dropped, not fatal |
| The 50/day tier is far too small for category scans (§6) | The UI says how far the quota goes, recommends the one-time $10, NVIDIA, or the hybrid mode, and the scout switches to «только непонятные» on its own |
| The key leaks | `.env` only, masked in the API, redacted from errors, never logged, not in backups without `include_secrets` |
