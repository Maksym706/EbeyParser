# AI models for EbeyParser: what to run on which hardware

Status: research and hands-on benchmark done on **2026-09-29**. The machine-readable version is
`ebeyparser/ai/model_catalog.py` (`recommend(ram_gb, vram_gb, arm)`), tested in `tests/test_model_catalog.py`.
Related: `docs/design/AI_SCOUT.md` (the triage pipeline that uses these models).

**Rule zero: small models never produce prices.** They read, classify and flag. Every euro comes from our own price
history (see AI_SCOUT.md §1). Section 7 lists the other risks.

---

## 1. Recommendations per tier

Speeds are for batched triage: 10 ads per call, about 120 prompt and 50 output tokens per ad, the compact prompt from §5,
and a strict JSON schema. "Measured" means our benchmark in §4 on a shared 4-vCPU VM, which is roughly N100 class. The
other speeds are extrapolated from memory bandwidth and published GPU numbers (§4.4), with an error of ±50 %.

| Tier | Hardware | Triage (every ad) | Vision (top candidates) | Embeddings (always on the server) | Second opinion (1–2 deals/day) |
|---|---|---|---|---|---|
| **T0** | CPU 2–4 cores, 4–8 GB (N100, old laptop, Pi 5 8 GB) | **Qwen3.5 2B** Q4_K_M, **batch 5**. Needs ~2.5–3 GB RAM. **~6 s/ad → ~600 ads/h** (measured). No model under 2B is usable for triage | Same model plus its vision projector (+0.7 GB), loaded on demand. **~38 s/photo** (measured, §4.3). With 4–5 GB RAM: Qwen3.5 0.8B + projector, 16 s/photo, good at reading text only. Better: the PC | **Qwen3-Embedding 0.6B** Q8_0 (~1 GB). With 4–5 GB RAM: EmbeddingGemma 300M | None locally. Use the PC, or Claude (already supported as `ai.second_opinion`) |
| **T1** | CPU 8 cores, 16–32 GB | **Qwen3.5 4B** Q4_K_M. Needs ~5 GB. ~6 s/ad → ~600 ads/h (est.; 15.5 s/ad measured on T0) | Qwen3.5 4B + projector (same download) | Qwen3-Embedding 0.6B | Qwen3.5 9B, thinking on. Minutes per deal, which is fine for 1–2 a day |
| **T2** | GPU 8–12 GB (RTX 3060 12 GB / 4060 8 GB) | **Qwen3.5 9B** Q4_K_M (5.7 GB + 0.9 GB projector). ~1.5 s/ad → ~2,400 ads/h | Same model, one load for text and photos | Qwen3-Embedding 0.6B (server CPU) | Same 9B with thinking on. No model swap |
| **T3** | GPU 24 GB (RTX 3090 / 4090) | **Qwen3.6 35B-A3B** MoE UD-Q4_K_M (~22 GB). ~0.5 s/ad, >100 tok/s on a 3090 | Same model (native vision) | Qwen3-Embedding 0.6B (server CPU) | **Qwen3.8 27B** Q4_K_M (17 GB), loaded on demand |
| **Split** (recommended) | T0/T1 server 24/7 + T2/T3 gaming PC, sometimes off | Server: T0/T1 pick | PC: T2/T3 pick. When the PC is off, photos wait in `vision_queue`, or go to the server's small model if the user allows it | Server only. Vectors are comparable only within one model, so the embedder must never move | PC. When it is off, Claude or skip |

### Exact ids

| Model | Ollama | LM Studio (`lms get …`) | llama.cpp (`llama-server -hf …`) | File at quant | RAM on CPU* | VRAM |
|---|---|---|---|---|---|---|
| Qwen3.5 0.8B (photos only) | `qwen3.5:0.8b-q4_K_M` | `qwen/qwen3.5-0.8b` | `unsloth/Qwen3.5-0.8B-GGUF:Q4_K_M` + `mmproj-F16.gguf` | 0.53 GB + 0.2 GB | 2.0 GB with projector (measured) | 1.5 GB |
| Qwen3.5 2B | `qwen3.5:2b-q4_K_M` (1.9 GB incl. vision) | `qwen/qwen3.5-2b` | `unsloth/Qwen3.5-2B-GGUF:Q4_K_M` (+ `mmproj-F16.gguf` for photos) | 1.28 GB + 0.67 GB projector | 3.1 GB measured | 2.5 GB |
| Qwen3.5 4B | `qwen3.5:4b-q4_K_M` | `qwen/qwen3.5-4b` | `unsloth/Qwen3.5-4B-GGUF:Q4_K_M` | 2.74 GB + 0.67 GB | 4.8 GB (6.6 GB peak RSS measured, incl. mmap page cache) | 4 GB |
| Qwen3.5 9B | `qwen3.5:9b-q4_K_M` (6.6 GB) | `qwen/qwen3.5-9b` | `unsloth/Qwen3.5-9B-GGUF:Q4_K_M` | 5.68 GB + 0.92 GB | ~9 GB | 8 GB |
| Qwen3.6 35B-A3B | `qwen3.6:35b-a3b-q4_K_M` (24 GB: partial CPU offload on a 24 GB card) | `qwen/qwen3.6-35b-a3b` | `unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M` (fits 24 GB) | ~22 GB | – | 24 GB |
| Qwen3.8 27B | `qwen3.8:27b` (18 GB, q4_K_M) | `qwen/qwen3.8-27b` | `unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M` | 16.5 GB + 0.93 GB | – | 20 GB |
| Qwen3-Embedding 0.6B | `qwen3-embedding:0.6b` (639 MB) | `Qwen/Qwen3-Embedding-0.6B-GGUF` | `Qwen/Qwen3-Embedding-0.6B-GGUF:Q8_0` + `--embedding` | 0.64 GB | ~1 GB | – |
| EmbeddingGemma 300M | `embeddinggemma` (622 MB) | `ggml-org/embeddinggemma-300M-GGUF` | `ggml-org/embeddinggemma-300M-GGUF:Q8_0` | 0.33 GB | ~0.5 GB | – |
| Qwen3-Reranker 0.6B | – (Ollama has no rerank API) | – | `ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF` + `--reranking` | 0.64 GB | ~1 GB | – |
| TranslateGemma 4B | `translategemma:4b` (3.3 GB) | from HF search | – | 3.3 GB | ~4.5 GB | 4 GB |

\* RAM of the model process: weights, plus an 8k-token KV cache, plus compute buffers. Qwen3.5's 248k-token vocabulary
makes the logits buffer large, so the process uses 2–2.5× the file size. `--no-mmap` avoids counting the file twice.
The app itself needs about 1 GB on top.

LM Studio ids are catalog ids. The quant is chosen in the app (Q4_K_M is the default).

---

## 2. Why these picks

### Triage: Qwen3.5 small (2B / 4B / 9B)

* **Newest dense small family, one model for text and photos.** Qwen3.5 0.8B/2B/4B/9B were released 2026-03-02. All
  are natively multimodal (early-fusion vision), have a 262k context, are Apache-2.0, and support thinking and
  non-thinking modes. Thinking is off by default on the small models. [S1][S2]
  This replaces our current pair of Qwen2.5-VL-7B and a separate text model.
* **Measured on our task** (§4), they are the only small models that stayed usable in 10-ad batches:
  * The 4B model scored product 97 %, kind 87 %, scam 100 %, JSON 100 %, and gave the reason in Russian every time.
  * The 2B model scored product 60 %, kind 63 %, scam 87 %, JSON 100 %, at 2.6× the 4B's speed.
  * The 0.8B model and Granite 4.2 3B broke down: renumbered or missing items, copied text, echoed the example.
* **Instruction following** per the model cards: IFEval 89.8 (4B) and 78.6 (2B); MMLU-Pro 79.1 and 66.5. The 4B beats
  the 2B on all 20 listed benchmarks, including the multilingual ones (MMMLU, MMLU-ProX, INCLUDE, WMT24++). [S3][S4]
* **Runtimes:** available everywhere: Ollama `qwen3.5:*`, LM Studio `qwen/qwen3.5-*`, Unsloth GGUFs. [S5][S6]
  GGUFs with MTP (multi-token prediction) heads (`*-mtp-q4_K_M`) promise faster decoding with llama.cpp's MTP
  support. We did not test them; this is the next speed lever for CPUs.
* **Runner-ups:**
  * **Gemma 4 E2B/E4B** (2026-04-02). Now Apache-2.0, with vision and audio, 128k context, 140+ languages. [S7]
    Tested in §4.2. Its effective 4.5B parameters sit on 8B stored weights (per-layer embeddings), so it is heavier on
    RAM than Qwen3.5 4B.
  * **Ministral 3** 3B/8B/14B (2025-12, Apache-2.0, vision, 256k context, European vendor). [S8]
  * **Granite 4.2** 3B/8B (2026-08-25, Apache-2.0; German is an officially tested language). It failed our batched
    format. [S9]
  * **LFM2.5** 1.2B / VL 1.6B (2026-01). Very fast, but under the LFM Open License (revenue cap). [S10]
  * **Phi-4-mini** 3.8B. English-first; no Phi-5 is out. [S11]
* **Qwen3.6/3.8** (April–September 2026) only ship 27B+ open weights, besides Qwen3.8-Flash-Next (125B-A6B, a
  community licence). They are for T3, not for the server. [S12][S13]

### Vision: the same Qwen3.5 model, bigger on the GPU

* Qwen3.5 4B and 9B are the strongest multimodal models under 15B per Artificial Analysis. They score 65.4 % and
  69.2 % on MMMU-Pro, versus 52.0 % for Qwen3-VL-4B and 46.0 % for Ministral 3 8B. [S1]
* On CPU (T0) the 2B model with its F16 projector reads a German iCloud lock screen and a GPU label (§4.3). That makes
  it usable for the 5–20 top candidates per day, not for every ad.
* For T3, Qwen3.6-35B-A3B has native vision with 3B active parameters, >100 tok/s on a 3090. [S14]
* Runner-ups:
  * Gemma 4 E4B/12B (vision on all sizes).
  * Qwen3-VL 2B/4B/8B (2025, still good at OCR).
  * MiniCPM-V 4.5 (8.7B, strong OCR).
  * LFM2.5-VL 1.6B/3B.
  * Moondream 2, SmolVLM (too weak for defect or lock judgments).

### Embeddings: Qwen3-Embedding 0.6B

* MMTEB (multilingual) mean(task) 64.33, against EmbeddingGemma-300M 61.15, multilingual-e5-large-instruct 63.22 and
  BGE-M3 59.56. 100+ languages, 32k context. [S15][S16]
* Other properties: Apache-2.0, instruction-aware, Matryoshka dims from 32 to 1024. Ollama and llama.cpp support it
  natively.
* Our texts are short titles in DE/RU/EN, so a 0.6B model costs ~0.1–0.3 s per title on a T0 CPU. That is negligible
  against triage.
* **EmbeddingGemma 300M** is the light fallback: 768 dims with Matryoshka, 2k context, under the Gemma Terms of Use.
* **jina-embeddings-v5-text-small/nano** (2026-02-18) score higher: MMTEB 67.7 and 65.5. [S17] Earlier Jina models
  were CC BY-NC, so check the licence before using them. Granite Embedding Multilingual R2 (2026) is another option.
* **Pick one embedder and keep it.** Switching means re-embedding the whole price history. So it runs on the always-on
  server, never on the sometimes-off PC.

### Second opinion and "LLM server for 1500 €" parts planning

* Use the largest model the PC holds, with thinking on:
  * Qwen3.5 9B (T2).
  * Qwen3.8 27B (T3). It is the newest open 27B (2026-09) and multimodal. [S13]
  * Claude through the existing `ai.second_opinion` when there is no GPU.
* For parts planning, the model proposes a **structure**: components, constraints, compatibility (socket, PSU watts,
  VRAM per GPU). **Prices come only from our listings and history.** Small models get hardware facts wrong (VRAM sizes,
  sockets), so let the model propose and the data decide. Never let a model under 9B write the plan.

### Optional: reranker, translation

* **Reranker:**
  * Qwen3-Reranker 0.6B (Apache-2.0) needs llama-server with `--reranking --pooling rank` and the `ggml-org` GGUF.
    Community GGUFs often lack the classifier tensors and return ~0 scores. [S18]
  * Ollama has no rerank endpoint.
  * With under 50 candidates, embeddings plus the triage model are enough. Add the reranker only for "find me X" over a
    large history.
* **DE→RU translation:**
  * Use the model that is already loaded (Qwen3.5 handles German to Russian well at 4B+) to avoid a model swap.
  * TranslateGemma 4B (2026-01, `translategemma:4b`) is better for long descriptions. It needs its own prompt format
    and is under the Gemma licence. [S19]
  * On T0, translate on demand only: 30–60 s per description.

---

## 3. What is new since 2025 and matters for us

1. **Native multimodal small models.**
   * Qwen3.5 0.8–9B (2026-03): one model does triage and photos. The old pairing of qwen2.5-vl-7b with a separate text
     model is obsolete.
   * Gemma 4 E2B/E4B (2026-04).
2. **Licences opened up.**
   * Gemma 4 is **Apache-2.0**. Gemma 3 used the restrictive Gemma licence.
   * Qwen3.5 and 3.6 are Apache-2.0.
   * Exceptions: Qwen3.8-Flash-Next (qwen-community), LFM (revenue cap), EmbeddingGemma and TranslateGemma (Gemma
     terms).
3. **Hybrid linear attention** (Qwen3.5 Gated DeltaNet, Nemotron 3.5 Mamba). Small KV caches, so long batches and a
   262k context cost little RAM.
4. **Fast MoE for 24 GB cards.**
   * Qwen3.6 35B-A3B (2026-04) and Nemotron 3.5 Lightning 30B-A3B (2026-08, text-only).
   * Both give 100+ tok/s on a 3090, so the PC reads every ad for free when it is on.
5. **Headless LM Studio.** `llmster` is a daemon without a GUI, currently LM Studio 0.4.24 (2026-09-09), with
   continuous batching. The server can run the same stack as the PC. [S20]
6. **MTP GGUFs** (Qwen3.5 `*-mtp-*`) plus llama.cpp MTP support. This is speculative decoding without a draft model;
   untested here.
7. **Embeddings.** jina v5 small/nano (2026-02) and Granite Embedding R2 (2026). Qwen3-Embedding 0.6B stays the
   Apache-2.0 sweet spot.

---

## 4. Hands-on CPU benchmark (2026-09-29)

### 4.1 Setup

* **Machine.** A 4-vCPU Intel Xeon (Sapphire Rapids, AVX-512/AMX) VM with 15 GB RAM, **shared with other agents'
  processes** (browser tests, Python test suites). The steal was visible, so absolute numbers are noisy. Measured
  speeds:
  * `llama-bench`, Qwen3.5 2B Q4_K_M, **generation: 3.4 tok/s on 1 thread, 5.4 on 2, 7.1 on 3, 7.1 on 4.** It is
    memory-bound and saturates at 3 threads.
  * Prompt processing at 4 threads: 120 tok/s.
  * This is **N100 class**, the T0 reference.
* **Runtime.** llama.cpp `master` 7fee178 (2026-09-29), built from source. Run as `llama-server -c 8192 -t 4
  --parallel 1` with the OpenAI API, `temperature 0`, `cache_prompt`, `enable_thinking: false`, and
  `response_format: json_schema` (strict).
* **Models.** Q4_K_M GGUFs, downloaded from Docker Hub's `ai/*` OCI model artifacts, which mirror Unsloth/IBM GGUFs.
  Hugging Face was blocked.
* **Test set.** 30 hand-written German Kleinanzeigen/eBay ads with gold labels.
  * Traps: prepayment/abroad/WhatsApp scams, "Kleinanzeigen Sicher bezahlen" phishing, iCloud lock, box-only, wanted,
    swap, spare part, accessory.
  * Gems: an attic Leica, an Omega from an estate, an underpriced gaming PC, a server, a Lego lot, a PS5 bundle.
  * Typos, lower-case text, and a vague title ("Grafikkarte Nvidia").
  * Kinds: 20 sale, 2 wanted, 1 swap, 3 defect, 1 part, 1 accessory, 1 box_only. 6 scams, 8 gems.
* **Metrics:**
  * JSON = the item is returned and matched by id.
  * Product = every gold token is in `product` (e.g. `iphone 13 pro 128`).
  * Kind = exact enum.
  * Scam = any scam signal vs gold.
  * gem AUC = P(interest of a gem > interest of a non-gem).
  * s/ad = wall clock / 30.

### 4.2 Results: triage

| Model (Q4_K_M) | Prompt | Batch | JSON | Product | Kind | Scam | Bundle | RU reason | gem AUC | out tok/ad | gen tok/s | **s/ad** | Peak RSS |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Qwen3.5 2B | v1 naive (pretty JSON, long keys) | 10 | 67 % | 57 % | **10 %** | 63 % | 50 % | 37 % | 0.44 | 120 | 9.4 | **14.1** | 3.1 GB |
| Qwen3.5 2B | v2 compact | 10 | **100 %** | 60 % | 63 % | 87 % | 80 % | 63 % | 0.43 | 46 | 9.0 | **5.9** | 3.1 GB |
| Qwen3.5 2B | v2 compact | 5 | **100 %** | 72 % | **80 %** | 88 % | 76 % | 60 % | 0.41 | 51 | 8.6‡ | **5.7‡** | 3.1 GB |
| **Qwen3.5 4B** | v2 compact | 10 | **100 %** | **97 %** | **87 %** | **100 %** | 80 % | **100 %** | 0.63 | 55 | 3.9 | **15.5** | 6.6 GB† |
| Qwen3.5 0.8B | v2 compact | 10 | 33 % | 30 % | 20 % | 30 % | 27 % | 3 % | 0.43 | 59 | 15.8 | 4.5 | 1.7 GB |
| Granite 4.2 3B | v2 compact | 10 | aborted: returned 1 of 10 items and echoed the example reason; then 0.2–0.5 tok/s under CPU contention | | | | | | | | | | 5.0 GB |
| Gemma 4 E4B | v2 compact | 10 | GEMMA_ROW |

† Peak RSS counts the mmap'd file pages and llama.cpp's repacked CPU copy of the weights. `--no-mmap` lowers it to
about the file size plus 1–2 GB.

‡ Batch 5 was scored on the first 25 ads (5 of 6 batches). The last batch hit a period of heavy CPU steal on the
shared VM (0.4–1 tok/s) and was stopped. Its s/ad is from the uncontended batch (1.9 s prompt + 26.8 s generation for
5 ads). With the system prompt cached, **batch 5 costs about the same per ad as batch 10** (51 vs 46 output tokens/ad)
**but the 2B is clearly more accurate at 5**: kind 80 % vs 63 %, product 72 % vs 60 %.

What the errors look like:

* **2B:** drops the variant ("MacBook Pro 14 M1 Pro" without 16/512, "Mac mini" without M2, "RAM Kit"). Calls a whole
  used item `part` (RAM kit, server, Mac mini). Misses the WhatsApp/e-mail scams. `interest` does not separate gems
  from junk (AUC 0.43).
* **4B:**
  * Only kind confusions at the edges: defect vs part for a broken phone or a dead e-bike battery, and AirPods called
    `accessory`.
  * One product string, "Crucial Ballistix DDR4 32GB 2x16GB", missed the "3200".
  * `interest` separates gems weakly (AUC 0.63).
  * **Every scam was caught**, including "Kleinanzeigen Sicher bezahlen: send me the link".
* **Naive prompt:** the model pretty-printed the JSON (120 tokens/ad) and hit `max_tokens` in batch 1. From ad 11
  onward the kinds collapsed into runs ("wanted, wanted, wanted…"). **The format fix alone made it 2.4× faster and
  kind accuracy 6× better.**

Take-aways for the product:

* The **2B model is a reader, not a judge.** Use it for product, kind and bundle, and let code (grounding, price
  history) decide. It is the T0 default only because the 4B is 2.6× slower there.
* The **4B model** is the first size whose `kind`, `scam` and `product` can be trusted without much code-side repair.
  Wherever the box can afford ~15 s/ad, meaning T1 or a quiet T0 with `mode: candidates`, use it.
* `interest` from models of 4B and under is weak. Rank by data (price vs history); use interest only as a tie-breaker
  and a "second look" trigger.

### 4.3 Results: vision (synthetic photos, CPU)

Four generated 768–1024 px JPEGs:

1. A German iCloud activation-lock screen ("iPhone gesperrt … Aktivierungssperre") photographed on a table.
2. A phone with a cracked black screen.
3. A GPU sticker ("MSI GeForce RTX 3060 VENTUS 2X 12G OC", S/N).
4. A catalogue-style render on white with marketing text ("Galaxy S24 Ultra – Galaxy AI is here").

The prompt asks for strict JSON: `text_seen`, `product`, `lock_screen`, `screen_damage`, `stock_photo`, `summary_ru`.
Score = correct booleans + expected text found (15 checks).

| Model | Score | s/photo (4 threads) | Peak RSS | Notes |
|---|---|---|---|---|
| Qwen3.5 0.8B + mmproj F16 (205 MB) | 11/15 | **15.8** (3 threads) | 2.0 GB | Perfect OCR: the whole German lock-screen text, the full GPU label with S/N. Judgement is weak: it called the cracked-phone photo a lock screen, and the catalogue render a real photo |
| Qwen3.5 2B + mmproj F16 (668 MB) | **13/15** | **37.9** (3 threads) | 4.0 GB | Lock screen, crack and catalogue render all right, perfect OCR. Its two misses were `stock_photo=true` on the synthetic crack and label images. Those *are* renders, so the gold label is debatable |

Image encoding dominates on CPU: a 768×1024 photo is ~750–1,100 prompt tokens for Qwen3.5, processed at 32–40 tok/s
by the 2B and ~100 tok/s by the 0.8B on this VM.

* **Send at most 2–3 photos at ≤768 px per ad on CPU**, and keep the 1024 px path for the GPU.
* The CPU vision budget is ~1–2 min per ad, fine for the top 5–20 candidates a day. The iCloud lock screen and a GPU
  sticker with its serial were read verbatim, which is the most valuable CPU-side check.
* Subtle damage (scratches, dead pixels, bent corners) on real photos needs the 9B+ model on the GPU. Synthetic images
  cannot prove that; treat a small model's `screen_damage=false` as "not seen", never as "no damage".

### 4.4 Normalising and extrapolating

On CPU:

> s/ad ≈ out_tokens_per_ad / gen_tok_s + new_prompt_tokens_per_ad / pp_tok_s

The system prompt is cached, so only the ~75–120 tokens of each ad count. Generation speed is memory-bandwidth bound:

> gen_tok_s ≈ effective_GB/s / model_GB

How that plays out per machine:

* **This VM:** ~8.5 GB/s effective at 4 threads, and **~4.3 GB/s per thread**. That gives 3.4 tok/s for the 2B at
  1 thread.
* **N100 / Pi 5 (T0):**
  * Single-channel DDR5 on an N100, or LPDDR4X on a Pi 5, gives 10–20 GB/s in practice. That is about 1–2× this VM
    for generation.
  * Prompt processing is weaker without AVX-512/AMX: an N100 does ~40–80 tok/s for a 2B.
  * Expect **2B ≈ 5–8 s/ad** and **4B ≈ 12–20 s/ad**. Published Pi 5 numbers: 2–5 tok/s for 1–3B, ~5–10 for ~1B. [S21]
* **8-core desktop (T1):** dual-channel DDR4/DDR5 at 35–60 GB/s gives ~2.5–4× this VM. Expect **2B ≈ 2–3 s/ad** and
  **4B ≈ 5–7 s/ad** (≈ 500–700 ads/h). 9B at ~15–20 s/ad is only for the second opinion.
* **GPUs:**
  * RTX 3060 12 GB runs 8–9B Q4 at ~40 tok/s. [S22]
  * RTX 3090 runs Qwen3.6-35B-A3B UD-Q4_K_M at 110–160 tok/s with full offload. [S14]
  * 50 output tokens/ad plus prompt comes to 0.3–1.5 s/ad.

The catalog's `sec_per_ad` uses these midpoints. The scout's adaptive batch and `mode: auto` absorb the error.

---

## 5. Prompt and format tips for small models (for the scout)

Measured, not folklore. The v1 → v2 change in §4.2 did all of this except one-letter keys.

1. **Demand minified JSON explicitly** and show a one-line example. Small models pretty-print by habit, and indentation
   was more than half of all output tokens (120 → 46 tokens/ad). The llama.cpp json_schema grammar allows whitespace,
   so the schema alone does not prevent it.
2. **Make "sale" the explicit default kind** and list each other kind with the German trigger words (Suche/Kaufe →
   wanted, Tausche → swap, "für Bastler"/"iCloud gesperrt" → defect, "nur OVP" → box_only). Without this, a 2B drifts
   into runs of the same wrong kind in long batches.
3. **Cap the Russian reason in words** ("max 12 words"). The 0.8B copied the German description into it; the 2B wrote
   non-Russian reasons 37 % of the time. The model's language skills cost tokens: ~1 token per 3–4 Cyrillic characters
   in Qwen's tokenizer. If budget is tight, make `reason_ru` optional or generate it only for promoted ads.
4. **Match by id, never by order.** The 0.8B renumbered items (id 7 for ad 30), and Granite returned 1 of 10. The
   scout's `parse_triage` already drops these and retries in halves, which is correct.
5. **Batch size: 5 for the 2B, 8–10 for the 4B and larger.** With a cached system prompt, per-ad cost barely depends
   on batch size (51 vs 46 output tokens/ad). The 2B loses accuracy in long batches: kind 80 % at batch 5 vs 63 % at
   batch 10. The system prompt must stay byte-identical across calls so `cache_prompt` makes it free. Put the variable
   ads last. Nothing under 2B is usable for triage at any batch size.
6. **Thinking off** for triage: `chat_template_kwargs: {"enable_thinking": false}` for llama.cpp and LM Studio;
   Ollama's `think: false`. Qwen3.5 small models default to off, but set it anyway. Some llama.cpp builds ignored the
   flag (issue #20182). [S23] Turn thinking on only for the second opinion.
7. **Always use grammar-constrained output**:
   * `response_format: json_schema` (strict) on LM Studio and llama.cpp.
   * `format: <schema>` on Ollama.
   It costs little on CPU (7.1 tok/s raw vs ~9 tok/s in-server with grammar, within noise) and turned JSON validity
   from 67 % to 100 %. The schema cannot fix wrong ids or copied text, which is why §5.4 matters.
8. **Never ask for prices, market values or "is this a good price".** Keep the ad's own price in the input so the
   model can flag "far too cheap for a new item" as a scam signal, and nothing more.
9. **Keep variant tokens verbatim.** Ask for "numbers like 128GB, 2x16GB, Gen9 copied from the ad". The 2B still
   drops them, and grounding (AI_SCOUT §5) must require them in the ad text anyway.
10. One-letter keys (as in `prompts_triage.py`) should save another ~30–40 % of output tokens. That is consistent with
    our numbers: keys were ~15 of the 46–55 tokens/ad. Keep the enum values as words; the model needs the semantics.

---

## 6. Running headless on the Linux server, and reaching the PC

### 6.1 llama.cpp server (leanest; recommended for T0)

Build it (`cmake -B build -DGGML_NATIVE=ON && cmake --build build -j --target llama-server`) or take a release binary.
It works on x86-64 and ARM64 (Pi 5). Then create `/etc/systemd/system/llama-scout.service`:

```ini
[Unit]
Description=llama.cpp scout model (EbeyParser)
After=network-online.target

[Service]
User=ebey
ExecStart=/opt/llama/llama-server -m /opt/models/Qwen3.5-2B-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8080 -c 8192 -t 4 --parallel 1 --no-mmap \
  --chat-template-kwargs '{"enable_thinking":false}' --alias qwen3.5-2b
Restart=always
Nice=10
CPUQuota=300%

[Install]
WantedBy=multi-user.target
```

* A second unit serves the embedder:
  `llama-server -m Qwen3-Embedding-0.6B-Q8_0.gguf --embedding --pooling last --port 8081 -c 2048`.
* `CPUQuota`/`Nice` keep the monitor and the web UI responsive: `-t` = cores − 1.
* `-hf unsloth/Qwen3.5-2B-GGUF:Q4_K_M` downloads directly when Hugging Face is reachable.
* For photos on the server, add `--mmproj mmproj-F16.gguf`.

In EbeyParser, set `ai.scout.provider: openai`, `base_url: http://127.0.0.1:8080/v1`, `model: qwen3.5-2b`.

### 6.2 Ollama

* Install with `curl -fsSL https://ollama.com/install.sh | sh`. It creates the `ollama` systemd unit and supports
  ARM64.
* Then `ollama pull qwen3.5:2b-q4_K_M` and `ollama pull qwen3-embedding:0.6b`.
* Settings go in `systemctl edit ollama`:
  * `Environment=OLLAMA_KEEP_ALIVE=24h` keeps the scout model loaded.
  * `OLLAMA_MAX_LOADED_MODELS=2` keeps the scout and the embedder loaded together.
  * `OLLAMA_NUM_PARALLEL=1`.
  * `OLLAMA_HOST=0.0.0.0:11434` only if another machine must reach it.
* Structured output goes through `format: <JSON schema>`. Use explicit quant tags: the bare `qwen3.5:2b` tag is a
  2.7 GB Q8 download. [S5]

### 6.3 LM Studio headless (`llmster`)

* Install with `curl -fsSL https://lmstudio.ai/install.sh | bash`.
* Run `lms get qwen/qwen3.5-2b`, then `lms load qwen/qwen3.5-2b --ttl 0` and `lms server start --bind 0.0.0.0
  --port 1234`.
* LM Studio documents a systemd startup task for `llmster` (daemon start → load model → start server). [S20]
* Good when the user already knows LM Studio from the PC. It is heavier than llama.cpp on a 4 GB box.

### 6.4 Server → PC over LAN or Tailscale

* **On the PC:**
  * LM Studio: Developer → Server Settings → **Serve on Local Network**, or `lms server start --bind 0.0.0.0`.
    Default port 1234.
  * Ollama: `OLLAMA_HOST=0.0.0.0:11434`.
  * Allow the port in the Windows firewall for the Private network, or only for the Tailscale interface.
* **LAN:** `ai.base_url: http://192.168.x.y:1234/v1`. Give the PC a DHCP reservation.
* **Tailscale** (PC and server in one tailnet):
  * Use `http://<pc-name>:1234/v1` via MagicDNS, or the 100.x address.
  * No port forwarding. The traffic is WireGuard-encrypted, so plain HTTP inside the tailnet is fine.
  * Never expose 1234 or 11434 to the internet; neither has authentication by default. LM Studio can require an API
    token; set `ai.api_key` if it is enabled.
* **PC sometimes off:**
  * The app already treats `ai` as optional. Photos wait in `vision_queue` (AI_SCOUT §2).
  * `/ai/detect` only probes localhost. For a remote PC, the settings page should test `GET <base_url>/models`.
  * Wake-on-LAN from the server is a cheap addition for the 1–2 daily second opinions.

---

## 7. Risks

| Risk | Seen where | Mitigation |
|---|---|---|
| **Price hallucination**: small models state confident, wrong market prices | known 1.5–7B behaviour; AI_SCOUT §1 | never ask; prices only from our history; the prompt says "never invent prices" |
| **Variant drift**: drops or invents storage/generation ("MacBook Pro 14 M1 Pro" without 16/512; "13" → "13 Pro") | 2B in §4.2 | grounding: every variant token must be in the ad text |
| **Kind collapse in long batches** (runs of "wanted"/"part") | naive prompt, 2B | the v2 prompt; the adaptive batch halves on parse failures; code rules for obvious keywords (Suche/Tausche/OVP) |
| **Index shift / echo / copied text** | 0.8B, Granite 3B | match by id, drop mismatches, retry smaller; never use models under 2B for triage |
| **Weak interest ranking** (AUC 0.43–0.63) | 2B/4B | rank by data; interest only as a trigger for a second look |
| **Missed scams** (WhatsApp, e-mail, "Sicher bezahlen" link) | 2B missed 4 of 6 | keyword/regex scam rules in code (they are cheap) plus the model; the 4B caught 6 of 6 |
| **Vision on CPU is slow** (~1 min/photo) and small VLMs miss subtle defects | §4.3 | top candidates only, ≤3 photos at ≤768 px; the GPU model when the PC is on; never auto-buy on a small VLM's "no damage" |
| **Resource starvation** of the monitor on a 4-core box | our VM, with other processes | `-t cores-1`, `Nice`, `CPUQuota`, `ai.scout.pass_share` / `max_per_hour` |
| **Embedding model change** invalidates stored vectors | design | store `embed_model` with each vector; re-embed in the background on change |
| **Licences** | Gemma terms (EmbeddingGemma, TranslateGemma), LFM (revenue cap), Qwen3.8-Flash-Next (community), jina (check) | defaults are all Apache-2.0 |
| **Runtime quirks**: `enable_thinking` ignored in some llama.cpp builds; bad reranker GGUFs; Ollama bare tags pull Q8 | [S18][S23] | pin quant tags; set thinking off in the request and the server flag; use `ggml-org` reranker GGUFs |

---

## 8. Sources (accessed 2026-09-29)

Hugging Face, Ollama, LM Studio, Reddit, arXiv and most blogs were **blocked by the sandbox's egress proxy**. The facts
below come from web-search result summaries (URLs listed), from the Docker Hub `ai/*` model cards and OCI manifests
(`hub.docker.com/v2/repositories/ai/<name>`; file sizes and licences from the manifests), and from our own benchmark.

* [S1] Artificial Analysis, "Qwen3.5 small models: Everything you need to know". https://artificialanalysis.ai/articles/qwen3-5-small-models (release 2026-03-02; MMMU-Pro numbers)
* [S2] Docker Hub `ai/qwen3.5` model card and manifests (Unsloth GGUFs, Apache-2.0, image input, sizes). https://hub.docker.com/r/ai/qwen3.5
* [S3] llm-stats, Qwen3.5-2B vs 4B. https://llm-stats.com/models/compare/qwen3.5-2b-vs-qwen3.5-4b ; Qwen/Qwen3.5-4B model card https://huggingface.co/Qwen/Qwen3.5-4B
* [S4] Qwen/Qwen3.5-2B model card. https://huggingface.co/Qwen/Qwen3.5-2B
* [S5] Ollama library qwen3.5 (tags 0.8b/2b/4b/9b, `*-q4_K_M`, sizes). https://ollama.com/library/qwen3.5/tags
* [S6] LM Studio catalog `qwen/qwen3.5-2b|4b|9b`, `qwen/qwen3.8-27b`, `google/gemma-4-e2b|e4b|12b|26b-a4b|31b`. https://lmstudio.ai/models
* [S7] Gemma 4 model card (Docker Hub mirror of the Unsloth card; Apache-2.0, E2B 2.3B effective / 5.1B with embeddings, E4B 4.5B / 8B, 128k context). https://hub.docker.com/r/ai/gemma4 ; https://ai.google.dev/gemma/docs/core/model_card_4 ; release date 2026-04-02: https://en.wikipedia.org/wiki/Gemma_(language_model)
* [S8] Ministral 3 (3B/8B/14B, Apache-2.0, vision, 2025-12-02). https://theaibench.ai/models/ministral-3/ ; https://arxiv.org/pdf/2601.08584
* [S9] IBM Granite 4.2 (2026-08-25; 3B/8B/30B; languages incl. German). https://www.ibm.com/granite/docs/models/granite4-2 ; https://huggingface.co/ibm-granite/granite-4.2-3b
* [S10] Liquid AI LFM2.5. https://www.liquid.ai/blog/introducing-lfm2-5-the-next-generation-of-on-device-ai
* [S11] Phi status 2026 (no Phi-5; Phi-4-reasoning-vision-15B 2026-03-04). https://www.frankx.ai/blog/phi-analysis-2026
* [S12] Qwen3.6-27B / 35B-A3B (2026-04). https://qwen.ai/blog?id=qwen3.6-35b-a3b ; https://qwen.ai/blog?id=qwen3.6-27b
* [S13] Qwen3.8 (27B dense open; Flash-Next 125B-A6B 2026-08-26, qwen-community licence). https://github.com/QwenLM/Qwen3.8 ; https://www.marktechpost.com/2026/08/26/alibabas-qwen-team-releases-qwen3-8-flash-next-a-125b-multimodal-moe-with-6b-active-parameters-previewing-the-qwen4-architecture/
* [S14] Qwen3.6-35B-A3B on an RTX 3090 (UD-Q4_K_M fits 24 GB, 110–160 tok/s). https://www.gilesthomas.com/2026/07/benchmarking-qwen-3-6-35b-moe-rtx-3090 ; https://insiderllm.com/guides/best-way-run-qwen-3-6-35b-moe-locally/
* [S15] Qwen3-Embedding model card, MMTEB table (0.6B = 64.33). https://hub.docker.com/r/ai/qwen3-embedding ; https://huggingface.co/Qwen/Qwen3-Embedding-0.6B
* [S16] EmbeddingGemma model card (MTEB Multilingual v2 61.15; MRL 768/512/256/128). https://hub.docker.com/r/ai/embeddinggemma
* [S17] jina-embeddings-v5-text (2026-02-18; small 67.7 / nano 65.5 MMTEB). https://jina.ai/news/jina-embeddings-v5-text-distilling-4b-quality-into-sub-1b-multilingual-embeddings/
* [S18] Qwen3-Reranker GGUF pitfalls and the `ggml-org` GGUF. https://gist.github.com/VooDisss/42bce4eb5c76d3c325633886c5e348ee ; https://huggingface.co/ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF
* [S19] TranslateGemma (4B/12B/27B, 55 languages, 2026-01). https://blog.google/innovation-and-ai/technology/developers-tools/translategemma/ ; https://ollama.com/library/translategemma
* [S20] LM Studio llmster headless and serve on network. https://lmstudio.ai/docs/developer/core/headless_llmster ; https://lmstudio.ai/docs/developer/core/server/serve-on-network
* [S21] LLMs on a Raspberry Pi 5 (2026). https://www.promptquorum.com/prompt-bites/local-llm-raspberry-pi-5 ; https://www.stratosphereips.org/blog/2025/6/5/how-well-do-llms-perform-on-a-raspberry-pi-5
* [S22] RTX 3060 12 GB, 8B Q4 ~42 tok/s. https://modelfit.io/gpu/rtx-3060/
* [S23] llama.cpp issue #20182 (`enable_thinking` for Qwen3.5); Unsloth Qwen3.5 guide. https://github.com/ggml-org/llama.cpp/issues/20182 ; https://unsloth.ai/docs/models/qwen3.5
* Nemotron 3.5 Lightning 30B-A3B (2026-08-11, Mamba-2 + MoE hybrid, text). https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16
* Structured outputs: Ollama https://docs.ollama.com/capabilities/structured-outputs ; a small-LLM JSON benchmark (Gemma 3 4B 100 % parse, Llama 3.2 3B ~50 %) https://ascentcore.com/2026/04/01/small-llm-performance-benchmark/

The benchmark scripts and raw answers were kept outside the repo (agent scratchpad). The test set is 30 German ads with
gold labels and can be turned into `tests/fixtures/triage_eval.json` if the scout needs a regression set.
