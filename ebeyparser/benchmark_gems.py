"""Hidden-gems benchmark: script-only vs. AI-first (the AI scout, docs/design/AI_SCOUT.md).

The standard synthetic Berlin market of `benchmark.py` (deals, normal ads and every trap) gets
extra "hidden gems" — ads whose value a keyword/regex pipeline can't see — and gem-shaped traps:

gems   typo            "Iphne 13 128gb", "Playstaion 5 Digital", "Samsnug Galxy S23"
       vague_text      "Handy zu verkaufen" — the model is only in the text
       unknown_model   "Grafikkarte von Nvidia, weiß nicht welche … steht RTX 3070 drauf"
       pc_gpu          "Alter Rechner vom Dachboden" with an RTX 3080 inside
       konvolut        "Konvolut Elektronik Nachlass": Switch OLED + AirPods Pro 2 + junk
       bundle          "Konsole mit Zubehör": PS5 Disc + 2 controllers + games
       wrong_category  a DJI Mini 3 listed under Haushalt as "Spielzeug Drohne"
traps  gem_pc_no_gpu   "Gaming PC ohne Grafikkarte (RTX 3080 ausgebaut)"
       gem_pc_wish     "PC mit GTX 1060 … suche eigentlich was mit RTX 3080"
       gem_lot_broken  "Konvolut defekte Handys für Bastler: iPhone 12, Galaxy S21"
       gem_wanted_pc   "Suche alten PC mit RTX 3080"
       gem_scam        "RTX 4090 neu 250 €, nur Versand, Vorkasse"

The real Monitor runs twice on the same world: without the scout (today's pipeline) and with
it, the scout's text model simulated by `SimTriageLLM` — it returns JSON TEXT through the real
TriageEngine (batching, broken/partial JSON, index shifts, retries, script fallback) — at three
qualities: oracle, noisy and a weak small model that hallucinates models and components and whose
interest score is pure noise (as measured for Qwen3.5 2B, docs/design/AI_MODELS.md §4).
Speed is simulated too (tokens/s of a GPU or a 4-core CPU): overflow, the hourly cap and the
"candidates" mode are exercised. Stage C (photos) is a gem-aware oracle vision fake.

Cloud profiles (docs/design/CLOUD_AI.md): `cloud_free50` / `cloud_free1000` put the scout AND the photo
check behind the real quota limiter of ai/cloud.py with OpenRouter's free limits — 20 requests a
minute, 50 (or 1000 after $10 of credits) a day, random upstream 429s — on the simulated clock. The
monitor then paces the scout over the day, switches it to «только непонятные» when the quota runs
low, keeps photos of the top candidates first and never sends eBay ads to the cloud.

    python -m ebeyparser.benchmark_gems --seed 1 --n 400
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from .ai.triage import TriageEngine
from .benchmark import (
    BERLIN,
    CATEGORY_BY_KEY,
    PRODUCT_BY_ID,
    SEVERE_TRAPS,
    CountingNotifier,
    FakeEbayAPI,
    FakeEbaySold,
    FakeKleinanzeigen,
    Market,
    OracleEvaluator,
    Product,
    SiteAd,
    Truth,
    _ai_verdict,
    _nice_price,
    _quiet_logs,
    _true_profit,
    benchmark_config,
    build_market,
    is_haggle_deal,
    is_true_deal,
)
from .db import Database
from .models import AIVerdict, Listing
from .monitor import Monitor
from .pricing.text import normalize

GEM_FLAVOURS = ("typo", "vague_text", "unknown_model", "pc_gpu", "konvolut", "bundle", "wrong_category")
GEM_TRAPS = ("gem_pc_no_gpu", "gem_pc_wish", "gem_lot_broken", "gem_wanted_pc", "gem_scam")
GEM_SHARE = 0.12  # gems per stream ad
GEM_TRAP_SHARE = 0.05
LATE_HINT_RATE = 0.15  # the telling detail only after the search-card snippet (~150 characters)
MODES = ("oracle", "noisy", "weak")
PC_CATEGORY = 225  # PC-Zubehör: where old PCs are listed in the synthetic world
SNIPPET = 150

# (identify, contents recall, hallucination, broken JSON, wrong kind, interest noise, trap catch, index shift)
QUALITY: dict[str, tuple[float, float, float, float, float, int, float, float]] = {
    "oracle": (1.0, 1.0, 0.0, 0.0, 0.0, 0, 0.95, 0.0),
    "noisy": (0.85, 0.8, 0.05, 0.08, 0.08, 2, 0.75, 0.03),
    "weak": (0.65, 0.6, 0.12, 0.2, 0.15, 3, 0.5, 0.06),
}
# simulated hardware: (prompt tokens/s, generated tokens/s); calibrate with the real smoke test
HARDWARE: dict[str, tuple[float, float]] = {
    "gpu_7b": (1200.0, 50.0),  # 7B Q4 on an RTX 3060 12GB
    "cpu_3b": (45.0, 9.0),  # 3B Q4 on a 4-core desktop CPU
    "cpu_1.5b": (110.0, 20.0),  # 1.5B Q4 on a 4-core desktop CPU
    # free cloud (a big MoE model behind a shared API): fast tokens, but a quota (CLOUD_PROFILES)
    "cloud_free50": (3000.0, 120.0),
    "cloud_free1000": (3000.0, 120.0),
    "cloud_nvidia": (2500.0, 90.0),
}
# cloud hardware -> (requests per minute, requests per UTC day, share of calls answered with a 429)
CLOUD_PROFILES: dict[str, tuple[int, int, float]] = {
    "cloud_free50": (20, 50, 0.12),  # OpenRouter free, < $10 of credits ever bought
    "cloud_free1000": (20, 1000, 0.12),  # OpenRouter free after a one-time $10
    "cloud_nvidia": (40, 0, 0.05),  # the NVIDIA API catalog: ~40 a minute, no daily cap
}
CLOUD_TEXT_MODEL = "nvidia/nemotron-3.5-lightning:free"
CLOUD_VISION_MODEL = "nvidia/nemotron-nano-12b-v2-vl:free"
MODE_HARDWARE = {"oracle": "gpu_7b", "noisy": "gpu_7b", "weak": "cpu_3b"}
# the model research (AI_MODELS.md §4): a small model's interest barely separates gems from junk
# (AUC 0.43 for Qwen3.5 2B) — the weak model's interest is pure noise here
INTEREST_IS_NOISE = frozenset({"weak"})

# parts of old PCs that our history never prices (so the engine's value is a lower bound)
_CPUS = (("Intel Core i7-8700K", 90.0), ("Intel Core i5-9600K", 60.0), ("AMD Ryzen 5 3600", 55.0),
         ("AMD Ryzen 7 3700X", 85.0), ("Intel Core i7-6700K", 50.0))
_RAMS = ("16GB DDR4 RAM", "32GB DDR4 RAM", "8GB DDR4 RAM")
_TYPOS = {"iPhone": ("Iphne", "iPone", "Ihpone"), "Samsung": ("Samsnug", "Samung"), "Galaxy": ("Galxy", "Galaxie"),
          "PlayStation": ("Playstaion", "Plasytation"), "PS5": ("PS 5", "Ps5"), "MacBook": ("Makbook", "Mackbook"),
          "Switch": ("Swich", "Switsch"), "Nintendo": ("Nintedo", "Nitendo"), "AirPods": ("Airpots", "Air Pods"),
          "Pixel": ("Pixl",), "Dyson": ("Dysen", "Daison")}


# ---------------------------------------------------------------------------
# The gem market
# ---------------------------------------------------------------------------


@dataclass
class GemInfo:
    parts: list[str] = field(default_factory=list)  # what is really inside (names a model would write)
    main: str = ""  # the main product's canonical name
    hint_late: bool = False  # the telling detail is not in the search-card snippet
    hallucination_bait: str = ""  # what a weak model likes to invent here


class _GemBuilder:
    def __init__(self, market: Market, seed: int):
        self.market = market
        self.rng = random.Random(f"gems:{seed}")
        self.next_id = 2_990_000_000 + (seed % 50) * 1_000_000
        self.info: dict[str, GemInfo] = {}

    def _id(self) -> str:
        self.next_id += self.rng.randint(3, 97)
        return str(self.next_id)

    def _ad(self, title: str, desc: str, price: float | None, truth: Truth, category_id: int, info: GemInfo,
            **kw: Any) -> SiteAd:
        rng = self.rng
        plz, district = rng.choice(BERLIN)
        ad = SiteAd(ad_id=self._id(), title=title, description=desc, category_id=category_id, price=price,
                    negotiable=price is not None and rng.random() < 0.6, is_free=False, postal_code=plz,
                    district=district, images=rng.randint(2, 6), truth=truth, stream=True,
                    seller_name=rng.choice(("Heinz", "Gisela", "Uwe", "Karin", "Jürgen", "Monika", "Dieter")),
                    shipping=rng.random() < 0.3, distance_km=round(rng.uniform(1, 25), 1),
                    posted=f"Heute, {rng.randint(7, 23):02d}:{rng.randint(0, 59):02d}",
                    attributes={"Zustand": rng.choice(("Gut", "In Ordnung"))})
        for key, value in kw.items():
            setattr(ad, key, value)
        self.info[ad.ad_id] = info
        return ad

    def _pick(self, cats: Sequence[str]) -> Product:
        return self.rng.choice([p for p in PRODUCT_BY_ID.values() if p.category in cats])

    def _price(self, value: float, lo: float = 0.3, hi: float = 0.62) -> float:
        return _nice_price(self.rng, value * self.rng.uniform(lo, hi))

    @staticmethod
    def _late(text: str, hint: str, late: bool) -> str:
        """Put `hint` early (inside the snippet) or after it."""
        if not late:
            return f"{hint} {text}"
        filler = ("Verkaufe wegen Haushaltsauflösung, alles aus dem Keller meiner Eltern, lange nicht benutzt, "
                  "stand zuletzt im Arbeitszimmer. Abholung in Berlin, gerne am Wochenende.")
        return f"{filler} {text} {hint}"

    def gem(self, flavour: str) -> SiteAd:
        rng = self.rng
        late = rng.random() < LATE_HINT_RATE
        if flavour == "typo":
            p = self._pick(("smartphone", "console", "audio", "tablet"))
            words = f"{p.brand} {p.model}".split()
            typo_words = [rng.choice(_TYPOS[w]) if w in _TYPOS and rng.random() < 0.9 else w for w in words]
            if typo_words == words:
                typo_words[-1] = typo_words[-1][:-1] + typo_words[-1][-1] * 2  # "Pixel 77"-free: double last char
            title = " ".join(typo_words[1:] if p.brand and rng.random() < 0.5 else typo_words)
            if p.spec and re.fullmatch(r"\d+\s?GB", p.spec):
                title += " " + p.spec.lower().replace(" ", "")
            desc = rng.choice(("Funktioniert einwandfrei, kleine Gebrauchsspuren.", "Akku gut, mit Ladekabel.",
                               "Wenig benutzt, Displayschutz drauf."))
            truth = Truth(p.pid, p.category, "gem", flavour, p.price, p.price, p.price, wording=title)
            return self._ad(title, desc, self._price(p.price, 0.45, 0.66), truth,
                            CATEGORY_BY_KEY[p.category].category_id, GemInfo([p.name], p.name, False))
        if flavour == "vague_text":
            p = self._pick(("smartphone", "tablet", "audio", "smartwatch", "camera"))
            noun = CATEGORY_BY_KEY[p.category].noun
            title = rng.choice((f"{noun} zu verkaufen", f"Altes {noun}", f"{noun} abzugeben", "Technik aus Nachlass"))
            desc = self._late("Funktioniert, bitte nur Abholung.", f"Es ist ein {p.name}.", late)
            truth = Truth(p.pid, p.category, "gem", flavour, p.price, p.price, p.price)
            return self._ad(title, desc, self._price(p.price, 0.42, 0.66), truth,
                            CATEGORY_BY_KEY[p.category].category_id, GemInfo([p.name], p.name, late))
        if flavour == "unknown_model":
            p = self._pick(("gpu",))
            brand = "AMD" if p.model.startswith("RX") else "Nvidia"
            title = rng.choice((f"Grafikkarte von {brand}", "Grafikkarte aus altem PC", "PC Grafikkarte gebraucht"))
            hint = f"Kenne mich nicht aus, auf der Karte steht {p.model}."
            desc = self._late("Lief bis zuletzt ohne Probleme.", hint, late)
            truth = Truth(p.pid, p.category, "gem", flavour, p.price, p.price, p.price)
            return self._ad(title, desc, self._price(p.price, 0.38, 0.62), truth, CATEGORY_BY_KEY["gpu"].category_id,
                            GemInfo([p.model], p.model, late))
        if flavour == "pc_gpu":
            gpu = self._pick(("gpu",))
            cpu, cpu_value = rng.choice(_CPUS)
            ram = rng.choice(_RAMS)
            value = (gpu.price + cpu_value + 60.0) * 0.85  # parting out: every part sold on its own
            title = rng.choice(("Alter Rechner vom Dachboden", "PC aus Nachlass", "Computer Tower gebraucht",
                                "Alter Gaming PC", "Rechner zu verschenken gegen kleines Geld"))
            hint = f"Drin ist {cpu}, {ram} und eine Grafikkarte {gpu.model}."
            desc = self._late("Läuft noch, Windows 10. Nur Abholung.", hint, late)
            truth = Truth(gpu.pid, "gpu", "gem", flavour, gpu.price, round(value, 2), round(value, 2))
            return self._ad(title, desc, self._price(value, 0.28, 0.55), truth, PC_CATEGORY,
                            GemInfo([gpu.model, cpu, ram], "", late))
        if flavour == "konvolut":
            a = self._pick(("console", "audio", "smartwatch"))
            b = self._pick(("audio", "smartphone", "tablet"))
            if b.pid == a.pid:
                b = PRODUCT_BY_ID["airpodspro2"] if a.pid != "airpodspro2" else PRODUCT_BY_ID["jblcharge5"]
            value = (a.price + b.price) * 0.85 + 15.0
            title = rng.choice(("Konvolut Elektronik Nachlass", "Elektronik Sammlung Dachbodenfund",
                                "Kiste Technik aus Haushaltsauflösung", "Konvolut Konsole Kopfhörer usw"))
            hint = f"Dabei: {a.name}, {b.name}, Kabel und Fernbedienungen."
            desc = self._late("Nur komplett abzugeben.", hint, late)
            truth = Truth(a.pid, a.category, "gem", flavour, a.price, round(value, 2), round(value, 2))
            return self._ad(title, desc, self._price(value, 0.3, 0.55), truth, CATEGORY_BY_KEY["household"].category_id,
                            GemInfo([a.name, b.name], "", late))
        if flavour == "bundle":
            p = PRODUCT_BY_ID[rng.choice(("ps5_disc", "ps5_digital", "switch_oled", "xbox_sx"))]
            value = (p.price + 2 * 40.0 + 45.0) * 0.9
            fam = {"ps5_disc": "PS5", "ps5_digital": "PS5 Digital", "switch_oled": "Switch OLED", "xbox_sx": "Xbox Series X"}[p.pid]
            title = rng.choice(("Konsole mit Zubehör", "Spielekonsole komplett", f"{fam} Paket"))
            hint = f"{p.name} mit 2 Controllern und 5 Spielen."
            desc = self._late("Alles funktioniert, Kinder spielen nicht mehr.", hint, late)
            truth = Truth(p.pid, p.category, "gem", flavour, p.price, round(value, 2), round(value, 2))
            return self._ad(title, desc, self._price(value, 0.4, 0.6), truth, CATEGORY_BY_KEY["console"].category_id,
                            GemInfo([p.name, "2x Controller"], p.name, late))
        # wrong_category
        p = self._pick(("drone", "camera", "gpu", "vr"))
        other = rng.choice([c for c in ("household", "tools", "audio") if c != p.category])
        title = rng.choice(("Spielzeug abzugeben", "Elektronik Gerät", "Technik aus Keller", f"{p.brand} Gerät"))
        desc = self._late("Kaum benutzt, im Karton.", f"Ist ein {p.name}.", late)
        truth = Truth(p.pid, p.category, "gem", flavour, p.price, p.price, p.price)
        return self._ad(title, desc, self._price(p.price, 0.42, 0.65), truth, CATEGORY_BY_KEY[other].category_id,
                        GemInfo([p.name], p.name, late))

    def trap(self, flavour: str) -> SiteAd:
        rng = self.rng
        gpu = PRODUCT_BY_ID[rng.choice(("rtx3080", "rtx3070", "rtx3080ti", "rtx4070"))]
        if flavour == "gem_pc_no_gpu":
            title = rng.choice(("Gaming PC ohne Grafikkarte", "PC Tower ohne GPU", "Alter Gaming Rechner"))
            desc = f"Gaming PC ohne Grafikkarte ({gpu.model} ausgebaut, schon verkauft). i5, 16GB RAM, Netzteil 650W."
            info = GemInfo(["Intel Core i5", "16GB DDR4 RAM"], "", False, hallucination_bait=gpu.model)
            value, price = 90.0, float(rng.choice((120, 150, 180)))
        elif flavour == "gem_pc_wish":
            title = rng.choice(("Gaming PC GTX 1060", "Alter Gaming Rechner", "PC für Einsteiger"))
            desc = f"Gaming PC mit GTX 1060 6GB, i5 und 8GB RAM. Suche eigentlich was mit {gpu.model}, daher Verkauf."
            info = GemInfo(["GTX 1060 6GB", "Intel Core i5", "8GB DDR4 RAM"], "", False, hallucination_bait=gpu.model)
            value, price = 160.0, float(rng.choice((180, 200, 220)))
        elif flavour == "gem_lot_broken":
            title = rng.choice(("Konvolut Handys für Bastler", "Defekte Handys Konvolut", "Handy Sammlung Ersatzteile"))
            desc = "Konvolut defekte Handys für Bastler: iPhone 12, Galaxy S21, alle mit Displayschaden, gehen nicht an."
            info = GemInfo(["Apple iPhone 12", "Samsung Galaxy S21"], "", False)
            value, price = 60.0, float(rng.choice((80, 100, 120)))
        elif flavour == "gem_wanted_pc":
            title = rng.choice(("Suche alten PC mit Grafikkarte", "Suche Gaming PC", "Kaufe alte PCs"))
            desc = f"Suche alten PC mit {gpu.model} oder ähnlich, zahle fair."
            info = GemInfo([gpu.model], "", False)
            value, price = 0.0, None
        else:  # gem_scam
            gpu = PRODUCT_BY_ID["rtx4070ti"]
            title = f"{gpu.model} neu OVP"
            desc = "Neu und originalverpackt, nur Versand, Zahlung per Vorkasse oder PayPal Freunde. Kein Abholen."
            info = GemInfo([gpu.model], gpu.model, False)
            value, price = 0.0, float(rng.choice((150, 180, 200)))
        truth = Truth(gpu.pid, "gpu", "trap", flavour, gpu.price, value, gpu.price)
        return self._ad(title, desc, price, truth, PC_CATEGORY, info)


@dataclass
class GemMarket:
    market: Market
    info: dict[str, GemInfo]
    gems: list[str]  # ad ids
    gem_traps: list[str]


def build_gem_market(seed: int = 1, n_listings: int = 400, passes: int | None = None) -> GemMarket:
    """The standard world of benchmark.build_market plus hidden gems and gem-shaped traps,
    arriving over the same passes."""
    base = build_market(seed, n_listings, passes)
    builder = _GemBuilder(base, seed)
    n_gems = max(len(GEM_FLAVOURS), round(n_listings * GEM_SHARE))
    n_traps = max(len(GEM_TRAPS), round(n_listings * GEM_TRAP_SHARE))
    extra = [builder.gem(GEM_FLAVOURS[i % len(GEM_FLAVOURS)]) for i in range(n_gems)]
    extra += [builder.trap(GEM_TRAPS[i % len(GEM_TRAPS)]) for i in range(n_traps)]
    builder.rng.shuffle(extra)
    order_base = max((a.order for a in base.stream), default=0) + 1
    for i, ad in enumerate(extra):
        ad.arrival = 1 + i * base.passes // max(1, len(extra))
        ad.order = order_base + i
    market = Market(base.seed, base.passes, base.stream + extra, base.background, base.sold, base.offers, base.warmup)
    return GemMarket(market, builder.info, [a.ad_id for a in extra if a.truth.kind == "gem"],
                     [a.ad_id for a in extra if a.truth.kind == "trap"])


# ---------------------------------------------------------------------------
# Simulated scout LLM (returns JSON text: the real parser and engine do the rest)
# ---------------------------------------------------------------------------

_AD_LINE_RE = re.compile(r"^\[(\d+)\] Titel: (.*?) \| Preis: (.*?)(?: \| (?:Kategorie|eBay)[^|]*)*(?: \| Text: (.*))?$")
_TRAP_KIND = {"accessory": "acc", "bundle": "bundle", "box_only": "box", "wanted": "wanted", "swap": "swap",
              "defect": "defect", "partial": "part", "gem_wanted_pc": "wanted", "gem_lot_broken": "lot",
              "gem_pc_no_gpu": "pc", "gem_pc_wish": "pc", "gem_scam": "single"}
_TRAP_RISK = {"scam": ["scam"], "scam_cheap": ["scam"], "too_good": ["scam"], "locked": ["locked"], "fake": ["fake"],
              "rent": ["rent"], "defect": ["defect"], "partial": ["missing"], "gem_lot_broken": ["defect"],
              "gem_scam": ["scam"], "gem_pc_no_gpu": ["missing"]}


class SimClock:
    """Simulated seconds (the engine's clock and wall clock in the benchmark)."""

    def __init__(self, start: float = 1_800_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


class SimTriageLLM:
    """ChatModel for the scout: reads the batch prompt, answers from the hidden truth with the
    quality of `mode`, and costs simulated time on `hardware`."""

    def __init__(self, gm: GemMarket, mode: str = "oracle", *, seed: int = 1, clock: SimClock | None = None,
                 hardware: str | None = None):
        if mode not in QUALITY:
            raise ValueError(f"mode must be one of {tuple(QUALITY)}")
        self.gm = gm
        self.mode = mode
        self.seed = seed
        self.clock = clock or SimClock()
        self.hardware = hardware or MODE_HARDWARE[mode]
        self.calls = 0
        self.ads = 0
        self.broken = 0
        self._system_cached = False
        self._by_title: dict[str, list[SiteAd]] = {}
        m = gm.market
        for ad in m.stream + m.warmup:
            self._by_title.setdefault(" ".join(ad.title.split()), []).append(ad)

    # -- ChatModel -------------------------------------------------------------
    async def chat_json(self, system: str, user: str, images: list[bytes] | None = None,
                        schema: dict | None = None) -> str:
        self.calls += 1
        rng = random.Random(f"{self.seed}:{self.mode}:{self.calls}:{len(user)}")
        q = QUALITY[self.mode]
        items: list[dict[str, Any]] = []
        lines = [line for line in user.splitlines() if line.startswith("[")]
        for line in lines:
            m = _AD_LINE_RE.match(line)
            if not m:
                continue
            idx, title, price_text, text = int(m.group(1)), m.group(2), m.group(3), m.group(4) or ""
            ad = self._find(title, price_text)
            items.append(self._item(idx, ad, title, text, rng))
        self.ads += len(items)
        if q[7] and rng.random() < q[7] and len(items) > 1:  # off-by-one indexes
            for item in items:
                item["i"] += 1
        answer = json.dumps({"items": items}, ensure_ascii=False)
        if rng.random() < q[3]:
            self.broken += 1
            answer = self._break(answer, rng)
        pp, tg = HARDWARE[self.hardware]
        prompt_tokens = len(user) / 3.5 + (0 if self._system_cached else len(system) / 3.5)
        self._system_cached = True
        self.clock.advance(prompt_tokens / pp + len(answer) / 3.2 / tg)
        return answer

    async def health(self) -> dict:
        return {"ok": True, "server_ok": True, "model_available": True}

    async def aclose(self) -> None:
        return None

    # -- helpers ---------------------------------------------------------------
    def _find(self, title: str, price_text: str) -> SiteAd | None:
        cands = self._by_title.get(" ".join(title.rstrip("…").split()), [])
        if not cands:
            key = title.rstrip("…").strip()
            cands = [a for t, ads in self._by_title.items() if t.startswith(key) for a in ads]
        digits = re.sub(r"\D", "", price_text.split("€")[0]) if "€" in price_text else ""
        for ad in cands:
            if digits and ad.price is not None and str(int(ad.price)) == digits:
                return ad
            if digits and ad.drop_price is not None and str(int(ad.drop_price)) == digits:
                return ad
        return cands[0] if cands else None

    @staticmethod
    def _break(answer: str, rng: random.Random) -> str:
        how = rng.choice(("truncate", "fence", "comma", "prose", "truncate"))
        if how == "truncate":
            return answer[: int(len(answer) * rng.uniform(0.35, 0.9))]
        if how == "fence":
            return f"Hier ist die Antwort:\n```json\n{answer}\n```"
        if how == "comma":
            return answer.replace("}", ",}").replace("]", ",]")
        return "Die Anzeigen sehen gut aus, besonders die erste."

    def _item(self, idx: int, ad: SiteAd | None, title: str, text: str, rng: random.Random) -> dict[str, Any]:
        q = QUALITY[self.mode]
        base = {"i": idx, "k": "other", "p": "", "n": 1, "c": [], "q": "", "z": "used", "h": [], "x": [],
                "s": 3, "r": "обычное объявление"}
        if ad is None:
            return base
        t = ad.truth
        p = PRODUCT_BY_ID.get(t.pid)
        info = self.gm.info.get(ad.ad_id)
        visible = normalize(f"{title} {text}")
        noise = rng.randint(-q[5], q[5]) if q[5] else 0

        def interest(v: int) -> int:
            if self.mode in INTEREST_IS_NOISE:
                return rng.randint(0, 10)
            return max(0, min(10, v + noise))

        if info is not None:  # a gem or a gem trap
            parts_seen = [x for x in info.parts if self._visible(x, visible)]
            if t.kind == "trap":
                caught = rng.random() < q[6]
                kind = _TRAP_KIND.get(t.flavour, "other")
                item = {**base, "k": kind, "x": _TRAP_RISK.get(t.flavour, []) if caught else [],
                        "s": interest(1 if caught else 7), "r": "ловушка" if caught else "возможно, ценное"}
                if kind in ("pc", "lot"):
                    contents = list(parts_seen)
                    if not caught and info.hallucination_bait:
                        contents.insert(0, info.hallucination_bait)  # the classic weak-model mistake
                    item["c"] = contents
                elif info.main:
                    item["p"] = info.main
                if t.flavour == "gem_scam" and not caught:
                    item["h"] = ["cheap"]
                return item
            if not parts_seen:  # the telling detail is not in the snippet
                return {**base, "k": "other" if t.flavour != "pc_gpu" else "pc", "s": interest(5),
                        "h": ["vague"], "r": "непонятно, что внутри"}
            identified = rng.random() < q[0]
            kind = {"pc_gpu": "pc", "konvolut": "lot", "bundle": "bundle"}.get(t.flavour, "single")
            if rng.random() < q[4]:
                kind = rng.choice(("other", "acc", "single", "bundle"))
            item = {**base, "k": kind, "s": interest(8 if identified else 5),
                    "h": {"typo": ["typo"], "vague_text": ["vague"], "unknown_model": ["unknown_model"],
                          "pc_gpu": ["pc_parts", "vague"], "konvolut": ["lot"], "bundle": ["bundle"],
                          "wrong_category": ["wrong_category"]}[t.flavour],
                    "r": {"pc_gpu": "старый ПК, внутри ценная видеокарта", "konvolut": "лот с ценными вещами",
                          "bundle": "консоль с контроллерами и играми"}.get(t.flavour, "ценная вещь, плохо описана")}
            if kind in ("pc", "lot", "bundle"):
                contents = [x for x in parts_seen if rng.random() < q[1]] or parts_seen[:1]
                if rng.random() < q[2] and p is not None:
                    contents = [self._hallucinate(x, rng) for x in contents]
                item["c"] = contents
                if kind == "bundle" and info.main:
                    item["p"] = info.main
                    item["c"] = [x for x in contents if x != info.main]
            elif identified and p is not None:
                name = info.main or p.name
                item["p"] = self._hallucinate(name, rng) if rng.random() < q[2] else name
                item["q"] = p.ai_query
            return item

        # the standard world: a careful model names the storage only when the ad does
        name = ""
        if p is not None:
            spec_seen = bool(p.spec) and normalize(p.spec).replace(" ", "") in visible.replace(" ", "")
            name = p.name if spec_seen or not p.spec else " ".join(x for x in (p.brand, p.model) if x)
        known = p is not None and self._visible(p.model, visible)
        kind = "single"
        risks: list[str] = []
        s = 5
        if t.kind == "trap":
            caught = rng.random() < q[6]
            if t.flavour in _TRAP_KIND and (caught or t.flavour in ("accessory", "bundle")):
                kind = _TRAP_KIND[t.flavour]
            if caught:
                risks = _TRAP_RISK.get(t.flavour, [])
                s = 1
            else:
                s = 6
            if t.flavour == "variant" and not caught:  # takes it for the pricier variant
                from .benchmark import VARIANT_PAIRS

                pricier = next((PRODUCT_BY_ID[a] for a, b in VARIANT_PAIRS if b == t.pid), None)
                if pricier is not None:
                    name = pricier.name
        elif t.kind == "deal":
            s = 8
        if rng.random() < q[4]:
            kind = rng.choice(("other", "single", "acc"))
        item = {**base, "k": kind, "x": risks, "s": interest(s), "r": "обычное объявление"}
        if known and rng.random() < q[0]:
            item["p"] = self._hallucinate(name, rng) if rng.random() < q[2] else name
            item["q"] = p.ai_query if p is not None else ""
        if kind == "bundle":
            item["c"] = [name] if name else []
        return item

    @staticmethod
    def _visible(name: str, visible: str) -> bool:
        toks = [t for t in normalize(name).split() if any(c.isdigit() for c in t) or len(t) >= 4]
        return bool(toks) and all(t in visible for t in toks[:2])

    @staticmethod
    def _hallucinate(name: str, rng: random.Random) -> str:
        """A weak model's typical slip: a neighbouring model number or a pricier variant."""
        swaps = (("3060", "3080"), ("3070", "3080"), ("3080", "3090"), ("4070", "4080"), ("12", "13"), ("13", "14"),
                 ("S21", "S23"), ("S22", "S23"), ("Air", "Pro"), ("Mini 3", "Mini 4 Pro"), ("Quest 2", "Quest 3"))
        for a, b in swaps:
            if a in name:
                return name.replace(a, b, 1)
        return name + (" Pro" if rng.random() < 0.5 else " Max")


# ---------------------------------------------------------------------------
# The free cloud: one quota for the scout and the photos, random upstream 429s
# ---------------------------------------------------------------------------


# Retry-After of an upstream 429 (seconds): mostly a short breath, sometimes a minute
RETRY_AFTER_CHOICES = (1, 2, 2, 3, 5, 10, 30, 60)
CLOUD_PHOTO_SECONDS = 3.0  # one photo check on a free 12B VL endpoint (answer + upload of 3 photos)


class _CloudClient:
    """What the monitor reads off a cloud client: the kind, the quota, the last quota refusal."""

    def __init__(self, quota: Any, rng: random.Random, p429: float, clock: SimClock | None = None,
                 seconds: float = 0.0):
        self.cloud = "openrouter"
        self.quota = quota
        self.rng = rng
        self.p429 = p429
        self.clock = clock
        self.seconds = seconds  # simulated time one call takes (the scout's is counted by SimTriageLLM)
        self.last_limited: Exception | None = None
        self.calls_429 = 0
        self.requests = 0  # requests the quota let through (all days of the simulation)

    async def gate(self, purpose: str) -> None:
        """Like VisionLLM._send: the quota first; an upstream 429 is retried once after a short pause,
        a long one fails fast (CloudLimited) and the caller takes the usual "AI down" path."""
        from .ai.cloud import CloudLimited

        self.last_limited = None
        try:
            for attempt in range(2):
                await self.quota.acquire(purpose)
                self.requests += 1
                if self.rng.random() >= self.p429:
                    if self.clock is not None:
                        self.clock.advance(self.seconds)
                    self.quota.note_success()
                    return
                self.calls_429 += 1
                if self.clock is not None:
                    self.clock.advance(0.3)
                wait = self.quota.note_429({"retry-after": str(self.rng.choice(RETRY_AFTER_CHOICES))})
                if attempt == 0 and wait <= self.quota.max_wait:
                    continue
                raise self.quota.blocked(purpose) or CloudLimited("Облако просит подождать", reason="429")
        except CloudLimited as exc:
            self.last_limited = exc
            raise


class SimCloudLLM(_CloudClient):
    """The scout's model on a free cloud endpoint (SimTriageLLM behind the quota)."""

    def __init__(self, inner: SimTriageLLM, quota: Any, *, seed: int, p429: float):
        super().__init__(quota, random.Random(f"{seed}:cloud-scout"), p429)
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def chat_json(self, system: str, user: str, images: list[bytes] | None = None,
                        schema: dict | None = None) -> str:
        await self.gate("triage")
        return await self.inner.chat_json(system, user, images, schema)

    async def health(self) -> dict:
        return await self.inner.health()

    async def aclose(self) -> None:
        return None


def make_cloud_quota(hardware: str, clock: SimClock) -> Any:
    from .ai.cloud import QuotaLimiter

    rpm, daily, _ = CLOUD_PROFILES[hardware]
    kind = "nvidia" if "nvidia" in hardware else "openrouter"

    async def sleep(seconds: float) -> None:
        clock.advance(seconds)

    return QuotaLimiter(f"bench-{hardware}", kind=kind, rpm=rpm, daily_limit=daily, clock=clock, wall=clock,
                        sleep=sleep, rng=random.Random(7))


# ---------------------------------------------------------------------------
# Vision fake that also knows the gems (stage C)
# ---------------------------------------------------------------------------


class GemVision(OracleEvaluator):
    def __init__(self, gm: GemMarket, mode: str = "oracle", *, seed: int = 1):
        preset = "oracle" if mode == "oracle" else "noisy"
        super().__init__(gm.market, preset, seed=seed)
        self.gm = gm
        self.quality = mode

    async def evaluate(self, listing: Listing, images: list[bytes], *, purpose: str = "resale",
                       estimate: Any = None, target_price: float | None = None, **kw: Any) -> AIVerdict:
        info = self.gm.info.get(listing.ad_id)
        ad = self.market.by_id.get(listing.ad_id)
        if info is None or ad is None:
            return await super().evaluate(listing, images, purpose=purpose, estimate=estimate,
                                          target_price=target_price, **kw)
        self.calls += 1
        self.called.append(listing.ad_id)
        rng = random.Random(f"{self.seed}:{ad.ad_id}:{self.quality}:gemvision")
        t = ad.truth
        p = PRODUCT_BY_ID[t.pid]
        conf = round(rng.uniform(0.75, 0.92), 2)
        model = f"gemvision-{self.quality}"
        if t.kind == "trap":
            caught = rng.random() < (0.95 if self.quality == "oracle" else 0.8)
            flags = {"gem_pc_no_gpu": ["Видеокарты нет — только корпус"], "gem_pc_wish": [],
                     "gem_lot_broken": ["Все телефоны неисправны"], "gem_wanted_pc": ["Автор ищет товар"],
                     "gem_scam": ["Только предоплата и пересылка"]}.get(t.flavour, [])
            item_type = {"gem_wanted_pc": "wanted"}.get(t.flavour, "complete_pc" if "pc" in t.flavour else "bundle")
            defects = {"gem_pc_no_gpu": ["missing_parts"], "gem_lot_broken": ["not_working", "screen_broken"]}.get(
                t.flavour, [])
            if not caught:
                flags, defects = [], []
            return _ai_verdict(product="Gaming PC" if "pc" in t.flavour else p.name, search_query=p.ai_query,
                               photo_matches_description=True, condition="defective" if defects and "not_working"
                               in defects else "used", red_flags=flags,
                               estimated_market_price=round(max(t.value, 30.0), 2),
                               verdict="skip" if caught else "maybe", confidence=conf, reasoning="Проверил фото.",
                               model=model, item_type=item_type, defects=defects, locked=False,
                               stock_photos=t.flavour == "gem_scam" and caught)
        item_type = {"pc_gpu": "complete_pc", "konvolut": "bundle", "bundle": "bundle"}.get(t.flavour, "single")
        name = info.main or ", ".join(info.parts)
        est = round(t.value * (1 + rng.uniform(-self.noise, self.noise)), 2)
        cost = listing.price or 0.0
        ratio = cost / max(est, 1.0)
        verdict = "buy" if ratio <= 0.72 else ("maybe" if ratio <= 0.9 else "skip")
        return _ai_verdict(product=name, search_query=p.ai_query if item_type == "single" else "",
                           photo_matches_description=True, condition="used", red_flags=[],
                           estimated_market_price=est, verdict=verdict, confidence=conf,
                           reasoning=f"На фото: {name}.", model=model, item_type=item_type,
                           variant={"model": p.model}, defects=[], locked=False, stock_photos=False)


class CloudGemVision(GemVision):
    """The photo check on the same free cloud quota (photos of the top candidates come first)."""

    def __init__(self, gm: GemMarket, mode: str, quota: Any, *, seed: int, p429: float, clock: SimClock | None = None):
        super().__init__(gm, mode, seed=seed)
        self.llm = _CloudClient(quota, random.Random(f"{seed}:cloud-vision"), p429, clock, CLOUD_PHOTO_SECONDS)
        self.limited = 0

    async def evaluate(self, listing: Listing, images: list[bytes], *, purpose: str = "resale",
                       estimate: Any = None, target_price: float | None = None, **kw: Any) -> AIVerdict:
        from .ai.cloud import CloudLimited, local_only_reason

        try:
            if local_only_reason():  # an eBay ad: never to the cloud
                raise CloudLimited("eBay-объявления в облако не отправляю", reason="ebay")
            await self.llm.gate("vision")
        except CloudLimited as exc:
            self.llm.last_limited = exc
            self.limited += 1
            return AIVerdict(verdict="maybe", confidence=0.0, reasoning=f"Нейросеть не проверила фото: {exc}",
                             model="cloud")
        return await super().evaluate(listing, images, purpose=purpose, estimate=estimate, target_price=target_price,
                                      **kw)


# ---------------------------------------------------------------------------
# Runner and scoring
# ---------------------------------------------------------------------------


@dataclass
class GemsResult:
    seed: int
    n_listings: int
    mode: str
    scout: bool
    hardware: str
    scored: int
    true_deals: int
    buys: int
    correct_buys: int
    precision: float | None
    recall: float | None
    gems: int
    gem_true_deals: int
    gem_buys: int  # correct buys among gems
    gem_recall: float | None
    gem_seen: int  # gems bought or rated "maybe" (shown in the feed)
    gem_seen_rate: float | None
    trap_buys: int
    trap_buys_by_type: dict[str, int]
    severe_trap_buys: int
    gem_trap_buys: int
    false_buys: list[dict[str, Any]]
    found_by_scout_buys: int
    by_flavour: dict[str, dict[str, int]]
    scout_read: int
    scout_overflow: int
    scout_failed: int
    scout_calls: int
    scout_seconds: float
    scout_broken_answers: int
    scout_promoted: int
    vision_calls: int
    details: int
    elapsed_s: float
    cloud_requests: int = 0  # cloud profiles: requests the quota let through (scout + photos)
    cloud_429: int = 0
    cloud_limited: int = 0  # photo checks the quota refused (the "фото не проверены" path)
    cloud_daily_limit: int = 0
    cloud_profile: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def gems_config(*, scout: bool, mode: str, overrides: dict[str, Any] | None = None,
                hardware: str | None = None) -> Any:
    extra: dict[str, Any] = {"ai": {"scout": {"enabled": scout, "mode": "auto", "batch_size": 8,
                                              "max_per_hour": 2000 if MODE_HARDWARE[mode] == "gpu_7b" else 500,
                                              "pass_share": 0.5}}}
    if hardware in CLOUD_PROFILES:  # a big cloud model: 16-20 ads per call (triage.batch_for_model)
        extra["ai"]["scout"].update(model=CLOUD_TEXT_MODEL, batch_size=0, max_batch=0, max_tokens=2600,
                                    max_per_hour=20000)
        extra["ai"].update(cloud="openrouter", model=CLOUD_VISION_MODEL)
    searches_extra = [{"name": "Категория: PCs", "query": "", "category_id": PC_CATEGORY, "location": "Berlin",
                       "radius_km": 30}]
    cfg = benchmark_config(category_scan=True, ai_enabled=True, ebay_api=True, overrides=extra | (overrides or {}))
    names = {s.name for s in cfg.searches}
    from .config import SearchConfig

    cfg.searches += [SearchConfig(**s) for s in searches_extra if s["name"] not in names
                     and not any(x.category_id == PC_CATEGORY and not x.query for x in cfg.searches)]
    return cfg


async def run_gems_async(seed: int = 1, n_listings: int = 400, mode: str = "oracle", *, scout: bool = True,
                         hardware: str | None = None, drain_passes: int = 3,
                         config_overrides: dict[str, Any] | None = None) -> GemsResult:
    started = time.perf_counter()
    gm = build_gem_market(seed, n_listings)
    market = gm.market
    config = gems_config(scout=scout, mode=mode, overrides=config_overrides, hardware=hardware)
    db = Database()
    ka = FakeKleinanzeigen(market)
    sold = FakeEbaySold(market)
    api = FakeEbayAPI(market)
    clock = SimClock()
    llm = SimTriageLLM(gm, mode, seed=seed, clock=clock, hardware=hardware)
    quota: Any = None
    scout_llm: Any = llm
    if hardware in CLOUD_PROFILES:
        quota = make_cloud_quota(hardware, clock)
        p429 = CLOUD_PROFILES[hardware][2]
        vision: GemVision = CloudGemVision(gm, mode, quota, seed=seed, p429=p429, clock=clock)
        scout_llm = SimCloudLLM(llm, quota, seed=seed, p429=p429)
    else:
        vision = GemVision(gm, mode, seed=seed)
    engine = TriageEngine.from_config(scout_llm, config.ai.scout, clock=clock, wall=clock) if scout else None
    notifier = CountingNotifier()
    monitor = Monitor(config, db, scraper=ka, ebay=sold, ebay_api=api, evaluator=vision,  # type: ignore[arg-type]
                      notifiers=[notifier], scout=engine)
    scored = list(market.stream)
    summaries: list[Any] = []
    interval = config.general.interval_minutes * 60
    with _quiet_logs():
        pass_no = -1
        while True:
            pass_no += 1
            if pass_no > market.passes + max(0, drain_passes):
                break
            ka.current_pass = api.current_pass = pass_no
            before = clock()
            summaries.append(await monitor.run_once())
            clock.advance(max(0.0, interval - (clock() - before)))  # the next pass starts an interval later
        await monitor.aclose()
    result = _score_gems(gm, scored, db, config, summaries, llm, vision, ka, api, seed, n_listings, mode, scout,
                         llm.hardware, time.perf_counter() - started)
    if quota is not None:
        clients = [vision.llm] + ([scout_llm] if isinstance(scout_llm, SimCloudLLM) else [])
        result.cloud_requests = sum(c.requests for c in clients)
        result.cloud_429 = sum(c.calls_429 for c in clients)
        result.cloud_limited = getattr(vision, "limited", 0)
        result.cloud_daily_limit = CLOUD_PROFILES[llm.hardware][1]
        result.cloud_profile = True
    return result


def run_gems(seed: int = 1, n_listings: int = 400, mode: str = "oracle", **kwargs: Any) -> GemsResult:
    return asyncio.run(run_gems_async(seed, n_listings, mode, **kwargs))


def _share(a: int, b: int) -> float | None:
    return a / b if b else None


def _score_gems(gm: GemMarket, scored: list[SiteAd], db: Database, config: Any, summaries: list[Any],
                llm: SimTriageLLM, vision: GemVision, ka: FakeKleinanzeigen, api: FakeEbayAPI, seed: int, n: int,
                mode: str, scout: bool, hardware: str, elapsed: float) -> GemsResult:
    pricing = config.pricing
    buys = correct = true_deals = 0
    gem_true = gem_buys = gem_seen = trap_buys = severe = gem_trap_buys = scout_buys = 0
    trap_types: Counter[str] = Counter()
    by_flavour: dict[str, Counter[str]] = {}
    false_buys: list[dict[str, Any]] = []
    gems = set(gm.gems)
    for ad in scored:
        ev = db.get_evaluation(ad.ad_id)
        verdict = ev.verdict if ev else "none"
        t = ad.truth
        # a VB ad that is a deal at the engine's own offer counts when the engine says "haggle"
        # (like the standard benchmark's «buy + торг»)
        deal = _is_deal(ad, pricing) or (is_haggle_deal(ad, pricing) and ev is not None and ev.action == "haggle")
        if not deal and ev is not None and ev.verdict == "buy" and ev.action == "haggle" and ev.offer_price:
            deal = t.kind != "trap" and _deal_at_price(ad, ev.offer_price, pricing)
        true_deals += deal
        if ad.ad_id in gems:
            c = by_flavour.setdefault(t.flavour, Counter())
            c["n"] += 1
            c["true_deal"] += deal
            c[verdict] += 1
            if ev is not None and ev.found_by == "ai_scout":
                c["found_by_scout"] += 1
            gem_true += deal
            gem_buys += deal and verdict == "buy"
            gem_seen += verdict in ("buy", "maybe")
        if verdict != "buy":
            continue
        buys += 1
        correct += deal
        scout_buys += bool(ev and ev.found_by == "ai_scout")
        if t.kind == "trap":
            trap_buys += 1
            trap_types[t.flavour] += 1
            severe += t.flavour in SEVERE_TRAPS or t.flavour.startswith("gem_")
            gem_trap_buys += t.flavour.startswith("gem_")
        if not deal:
            false_buys.append({"ad_id": ad.ad_id, "title": ad.title, "kind": t.kind, "flavour": t.flavour,
                               "price": ad.final_price, "true_value": t.value,
                               "estimate": ev.estimate.market_price if ev else None,
                               "source": ev.estimate.source if ev else "", "found_by": ev.found_by if ev else "",
                               "reasons": (ev.reasons[:4] if ev else [])})
    sums: dict[str, float] = Counter()
    for s in summaries:
        for name in ("scout_read", "scout_overflow", "scout_failed", "scout_calls", "scout_promoted", "ai_calls",
                     "details_fetched"):
            sums[name] += getattr(s, name, 0) or 0
        sums["scout_seconds"] += getattr(s, "scout_seconds", 0.0) or 0.0
    return GemsResult(
        seed=seed, n_listings=n, mode=mode, scout=scout, hardware=hardware, scored=len(scored),
        true_deals=true_deals, buys=buys, correct_buys=correct, precision=_share(correct, buys),
        recall=_share(correct, true_deals), gems=len(gems), gem_true_deals=gem_true, gem_buys=gem_buys,
        gem_recall=_share(gem_buys, gem_true), gem_seen=gem_seen, gem_seen_rate=_share(gem_seen, len(gems)),
        trap_buys=trap_buys, trap_buys_by_type=dict(trap_types), severe_trap_buys=severe,
        gem_trap_buys=gem_trap_buys, false_buys=false_buys[:20], found_by_scout_buys=scout_buys,
        by_flavour={k: dict(v) for k, v in sorted(by_flavour.items())},
        scout_read=int(sums["scout_read"]), scout_overflow=int(sums["scout_overflow"]),
        scout_failed=int(sums["scout_failed"]), scout_calls=int(sums["scout_calls"]),
        scout_seconds=round(float(sums["scout_seconds"]), 1), scout_broken_answers=llm.broken if scout else 0,
        scout_promoted=int(sums["scout_promoted"]), vision_calls=vision.calls, details=int(sums["details_fetched"]),
        elapsed_s=round(elapsed, 1),
    )


def _deal_at_price(ad: SiteAd, price: float, pricing: Any) -> bool:
    profit, roi = _true_profit(ad.truth.value, price + (ad.shipping_cost or 0.0), pricing)
    return profit >= pricing.min_profit and roi >= pricing.min_roi


def _is_deal(ad: SiteAd, pricing: Any) -> bool:
    if ad.truth.kind == "gem":
        price = ad.final_price
        if ad.is_free or price is None or price <= 1:
            return False
        profit, roi = _true_profit(ad.truth.value, price + (ad.shipping_cost or 0.0), pricing)
        return profit >= pricing.min_profit and roi >= pricing.min_roi
    return is_true_deal(ad, pricing)


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f} %"


def format_comparison(rows: list[GemsResult]) -> str:
    lines = ["Скрытые находки: только скрипт против ИИ-разведчика", "",
             f"{'режим':<10}{'разведчик':<21}{'точность':>9}{'полнота':>9}{'находки: buy':>14}{'в ленте':>9}"
             f"{'ловушки':>9}{'нейросеть нашла':>17}{'прочитал/не успел':>19}"]
    for r in rows:
        lines.append(
            f"{r.mode:<10}{('да, ' + r.hardware) if r.scout else 'нет':<21}{_pct(r.precision):>9}{_pct(r.recall):>9}"
            f"{f'{r.gem_buys}/{r.gem_true_deals}':>14}{_pct(r.gem_seen_rate):>9}{r.trap_buys:>9}"
            f"{r.found_by_scout_buys:>17}{f'{r.scout_read}/{r.scout_overflow}':>19}")
    cloud = [r for r in rows if r.cloud_profile]
    if cloud:
        lines.append("")
        lines.append("Облако: запросов за симуляцию / лимит в день · ответов 429 · фото не проверены из-за лимита")
        for r in cloud:
            lines.append(f"  {r.mode:<8}{r.hardware:<16}{r.cloud_requests:>5} / {r.cloud_daily_limit or '—':<6}"
                         f"{r.cloud_429:>6}{r.cloud_limited:>8}")
    lines.append("")
    for r in rows:
        if r.false_buys:
            lines.append(f"Ложные «покупать» ({r.mode}, разведчик {'да' if r.scout else 'нет'}):")
            for fb in r.false_buys[:5]:
                lines.append(f"  · {fb['title'][:60]} — {fb['kind']}/{fb['flavour']}, цена {fb['price']}, "
                             f"истинная {fb['true_value']}, оценка {fb['estimate']} ({fb['source']}, {fb['found_by']})")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--n", type=int, default=400)
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--hardware", default=None, choices=sorted(HARDWARE))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    rows: list[GemsResult] = []
    for mode in [m for m in args.modes.split(",") if m]:
        rows.append(run_gems(args.seed, args.n, mode, scout=False))
        rows.append(run_gems(args.seed, args.n, mode, scout=True, hardware=args.hardware))
    if args.json:
        print(json.dumps([r.as_dict() for r in rows], ensure_ascii=False, indent=1))
    else:
        print(format_comparison(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
