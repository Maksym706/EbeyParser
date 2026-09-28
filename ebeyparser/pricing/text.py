"""Text helpers for German classifieds: normalization, search queries, red flags."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_TRANSLIT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
# "16 GB" -> "16gb" so that keywords and titles agree regardless of spacing
_UNIT_MERGE_RE = re.compile(r"\b(\d+(?:\.\d+)?) (gb|tb|mb|ghz|mhz|hz|mah|zoll|inch|mm)\b")


def _fold_char(ch: str) -> str:
    """Strip accents from Latin letters (é -> e); keep other scripts intact."""
    if ch.isascii():
        return ch
    decomposed = unicodedata.normalize("NFKD", ch)
    base = "".join(c for c in decomposed if not unicodedata.combining(c))
    return base if base and base.isascii() else ch


def _prepare(text: str) -> str:
    """Lowercase + German transliteration + accent folding (punctuation kept)."""
    t = unicodedata.normalize("NFKC", text).lower().translate(_TRANSLIT)
    return "".join(_fold_char(c) for c in t)


def normalize(text: str) -> str:
    """Lowercase, ä->ae ö->oe ü->ue ß->ss, strip punctuation/emojis, collapse whitespace.

    Decimal/thousand separators between digits survive as "." ("2,5" -> "2.5"),
    and number+unit pairs are glued ("16 GB" -> "16gb").
    """
    if not text:
        return ""
    t = _prepare(text)
    n = len(t)
    out: list[str] = []
    for i, ch in enumerate(t):
        if ch.isalnum():
            out.append(ch)
        elif ch in ".," and 0 < i < n - 1 and t[i - 1].isdigit() and t[i + 1].isdigit():
            out.append(".")
        else:
            out.append(" ")
    collapsed = " ".join("".join(out).split())
    return _UNIT_MERGE_RE.sub(r"\1\2", collapsed)


# ---------------------------------------------------------------------------
# Search query for comparables
# ---------------------------------------------------------------------------

_PRICE_RE = re.compile(
    r"(?:€|\beur\b|\beuro\b)\s*\d[\d.,]*"
    r"|\d[\d.,]*\s*(?:€|\beur\b|\beuro\b|,-|\.-)"
    r"|\b(?:np|uvp|neupreis|vb|fp|preis)\s*:?\s*\d[\d.,]*",
    re.IGNORECASE,
)

_FILLER = frozenset(
    """
    verkaufe verkauf verkaufen biete bieten abzugeben gebe ab
    top topzustand zustand sehr gut gute guter gutes gutem super mega hammer
    wie neu neue neuer neues neuem neuwertig neuwertige neuwertiger neuwertigem neuwertiges
    nagelneu brandneu ovp originalverpackt originalverpackung verpackung karton
    unbenutzt ungeoeffnet ungebraucht versiegelt sealed boxed
    inkl inklusive incl mit und u oder fuer ohne von vom zum zur im in am an auf aus bei bis
    der die das den dem des ein eine einen einem einer ist sind wird werden zu ich wir sie
    gebraucht benutzt genutzt wenig kaum selten fast nur voll
    np vb fp festpreis preis preisvorschlag preisvorschlaege verhandelbar verhandlungsbasis
    angebot schnaeppchen guenstig billig dringend schnell sofort wegen umzug
    rechnung garantie gewaehrleistung tausch versand abholung abzuholen privat privatverkauf
    original orginal funktionsfaehig funktionstuechtig einwandfrei tadellos gepflegt
    nichtraucher nichtraucherhaushalt tierfrei kratzerfrei kratzer
    euro eur stueck stk bitte lesen defekt defekte bastler
    new used like mint condition sale for with and the brand free shipping
    """.split()
)

_LOW_VALUE = frozenset(
    """
    gaming gamer oc edition grafikkarte handy smartphone laptop notebook konsole pc computer
    set bundle paket zubehoer ram ssd hdd speicher festplatte arbeitsspeicher box cpu prozessor
    farbe version modell model generation gen
    """.split()
)

_COLORS = frozenset(
    """
    schwarz weiss grau silber gold rot blau gruen gelb rosa pink lila space spacegrau
    black white grey gray silver red blue green midnight starlight graphite
    """.split()
)

_BRANDS = frozenset(
    """
    apple samsung sony nintendo microsoft lenovo dell hp asus acer msi gigabyte zotac evga palit
    gainward inno3d pny sapphire powercolor xfx asrock nvidia amd intel corsair kingston crucial
    wd seagate synology qnap logitech razer steelseries bose jbl sennheiser beyerdynamic canon
    nikon fujifilm fuji olympus panasonic lumix gopro dji garmin fitbit huawei xiaomi oneplus
    google lg philips dyson bosch makita dewalt festool metabo hilti miele siemens vorwerk
    kitchenaid delonghi jura nespresso lego playmobil marshall fender gibson yamaha roland korg
    shimano raspberry ubiquiti avm fritz meta oculus valve noctua nzxt thermaltake seasonic
    teufel nubert denon marantz onkyo sonos nothing honor oppo motorola nokia framework
    """.split()
)

_FAMILY = frozenset(
    """
    iphone ipad macbook mac mini pro max air ultra plus studio imac airpods watch galaxy tab note
    pixel thinkpad xps latitude elitebook zenbook vivobook rog strix tuf legion ideapad yoga
    surface rtx gtx rx radeon geforce ryzen core xeon threadripper epyc ps5 ps4 playstation
    xbox series switch oled lite steam deck quest hero mavic kindle alpha eos thinkcentre
    optiplex nuc titan quadro founders
    """.split()
)

# " + 2 Controller", " inkl. Spiele": accessories after these markers are not the product
_EXTRAS_RE = re.compile(r"\s(?:\+|&|inkl\.?|incl\.?|inklusive|zzgl\.?)\s", re.IGNORECASE)

_UNIT_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?(?:gb|tb|mb|ghz|mhz|hz|mah|zoll|inch|mm|w|k|p|mp|l|kg|cm)")


def _query_token_score(tok: str, first_unit: bool) -> float:
    if tok in _LOW_VALUE:
        return 1.0
    if _UNIT_TOKEN_RE.fullmatch(tok):
        return 2.0 if first_unit else 1.5
    if any(c.isdigit() for c in tok):
        return 5.0  # model numbers: 3090, m1, i7, 8700k, ps5
    if tok in _BRANDS:
        return 4.0
    if tok in _FAMILY:
        return 3.0
    if tok in _COLORS:
        return 0.8
    return 2.5


_GPU_CORE_RE = re.compile(r"\b(rtx|gtx|rx|arc)\s*([a-z]?\d{3,4})\s*(ti|super|xtx|xt)?\b")
_MODEL_SUFFIXES = frozenset({"ti", "super", "xt", "xtx", "pro", "max", "plus", "mini", "ultra"})


def _gpu_query(title: str) -> str | None:
    """Graphics cards: board partner and cooler don't matter much for the price —
    'MSI NVIDIA RTX 3080 Ti VENTUS 3X 12G OC' -> 'rtx 3080 ti'."""
    m = _GPU_CORE_RE.search(normalize(title))
    return " ".join(part for part in m.groups() if part) if m else None


def make_search_query(title: str, max_words: int = 5) -> str:
    """Turn a messy German ad title into a short query for finding comparables."""
    if not title or max_words <= 0:
        return ""
    gpu = _gpu_query(title)
    if gpu:
        return gpu
    cleaned = _PRICE_RE.sub(" ", unicodedata.normalize("NFKC", title))
    extras = _EXTRAS_RE.search(cleaned)
    if extras:
        head = cleaned[: extras.start()]
        if sum(1 for t in normalize(head).split() if t not in _FILLER) >= 2:
            cleaned = head
    tokens: list[str] = []
    for tok in normalize(cleaned).split():
        if tok in _FILLER or tok in tokens:
            continue
        if len(tok) == 1 and tok.isalpha() and tok != "x":
            continue
        tokens.append(tok)
    if not tokens:
        return " ".join(normalize(title).split()[:max_words])

    scored: list[tuple[float, int, str]] = []
    seen_unit = False
    for idx, tok in enumerate(tokens):
        is_unit = bool(_UNIT_TOKEN_RE.fullmatch(tok))
        scored.append((_query_token_score(tok, not seen_unit), idx, tok))
        seen_unit = seen_unit or is_unit
    # Enough strong tokens -> drop generic words / colours entirely.
    if sum(1 for s, _, _ in scored if s >= 2.5) >= 3:
        scored = [t for t in scored if t[0] >= 1.5]
    best = sorted(scored, key=lambda t: (-t[0], t[1]))[:max_words]
    # never lose a model suffix right after a model number ("13 pro max", "3080 ti")
    chosen = {idx for _, idx, _ in best}
    for _, idx, tok in list(best):
        nxt = idx + 1
        while nxt < len(tokens) and tokens[nxt] in _MODEL_SUFFIXES and any(c.isdigit() for c in tok):
            if nxt not in chosen:
                if len(best) >= max_words:
                    weakest = max((b for b in best if b[1] != idx), key=lambda b: (b[0] * -1, b[1]))
                    best.remove(weakest)
                    chosen.discard(weakest[1])
                best.append((0.0, nxt, tokens[nxt]))
                chosen.add(nxt)
            nxt += 1
    return " ".join(tok for _, _, tok in sorted(best, key=lambda t: t[1]))


# ---------------------------------------------------------------------------
# Wanted ads
# ---------------------------------------------------------------------------

_WANTED_FIRST = frozenset({"suche", "suchen", "sucht", "kaufe", "ankauf", "gesucht", "wtb", "wanted"})
_WANTED_ANYWHERE = frozenset({"gesucht", "ankauf", "wanted"})
_OFFER_FIRST = frozenset({"verkaufe", "verkauf", "biete", "tausche", "tausch"})


def is_wanted_ad(title: str) -> bool:
    """True for "Suche ...", "Kaufe ...", "Ankauf", "... gesucht" — someone buying, not selling."""
    tokens = normalize(title).split()
    if not tokens:
        return False
    if tokens[0] in _WANTED_FIRST:
        return True
    if tokens[:2] in (["wir", "kaufen"], ["ich", "kaufe"], ["ich", "suche"], ["wir", "suchen"]):
        return True
    if _WANTED_ANYWHERE.intersection(tokens):
        return True
    return "suche" in tokens and tokens[0] not in _OFFER_FIRST


# ---------------------------------------------------------------------------
# Red flags
# ---------------------------------------------------------------------------

FLAG_WANTED = "Это не продажа, а поиск"
FLAG_DEFECT = "Дефект / для мастера"
FLAG_BOX_ONLY = "Только коробка"
FLAG_LOCKED = "Заблокировано (iCloud/аккаунт)"
FLAG_FAKE = "Реплика / подделка"
FLAG_SCAM = "Признаки мошенничества"
FLAG_SWAP = "Только обмен"
FLAG_RENT = "Рассрочка/аренда"
FLAG_MISSING = "Нет комплектующих"
FLAG_SIMLOCK = "Привязка к оператору (SIM-lock)"
FLAG_UNTESTED = "Не проверено продавцом"
FLAG_WHATSAPP = "Просит связь через WhatsApp"
FLAG_RESERVED = "Возможно, уже зарезервировано"

SEVERE_FLAGS: frozenset[str] = frozenset(
    {FLAG_WANTED, FLAG_DEFECT, FLAG_BOX_ONLY, FLAG_LOCKED, FLAG_FAKE, FLAG_SCAM, FLAG_SWAP, FLAG_RENT}
)

_PUNCT = frozenset(":?!.,;")
_NEG_BEFORE = frozenset(
    "kein keine keinen keiner keinem keines keinerlei nicht nichts nix ohne nie niemals "
    "null frei no not non never without".split()
)
_STOP_BEFORE = frozenset("aber jedoch leider allerdings sondern doch but however".split())
_CONNECTORS = frozenset("und oder bzw sowie u and or".split())
_NEG_AFTER_DIRECT = frozenset({"frei", "free", "nein"})
_NEG_AFTER_COLON = frozenset("keine kein keiner keinerlei nein no none nicht nichts 0 ohne".split())
_NEG_AFTER_PHRASE = frozenset(
    "vorhanden moeglich akzeptiert erwuenscht gewuenscht festgestellt bekannt".split()
)

# up to two words that are not a negation (so "display nicht gebrochen" is no match)
_GAP2 = r"(?:(?!(?:nicht|nichts|kein\w*|ohne|nie|no|not)(?!\S))[^\W_]+ ){0,2}"
_GAP4 = r"(?:(?!(?:nicht|nichts|kein\w*|ohne|nie|no|not)(?!\S))[^\W_]+ ){0,4}"


@dataclass(frozen=True)
class _Rule:
    label: str
    patterns: tuple[str, ...]
    exclude_near: frozenset[str] = frozenset()  # context words that cancel a hit
    post_ok: frozenset[str] = frozenset()  # words right after the hit that cancel it
    near_after: bool = True  # also look at words after the hit for exclude_near
    compiled: tuple[re.Pattern[str], ...] = field(default=(), compare=False)


def _rule(
    label: str,
    patterns: list[str],
    exclude_near: str = "",
    post_ok: str = "",
    near_after: bool = True,
) -> _Rule:
    compiled = tuple(re.compile(rf"(?<!\S)(?:{p})(?!\S)") for p in patterns)
    return _Rule(
        label,
        tuple(patterns),
        frozenset(exclude_near.split()),
        frozenset(post_ok.split()),
        near_after,
        compiled,
    )


_PACKAGING = "ovp karton verpackung box schachtel packung originalverpackung huelle tasche"

_RULES: tuple[_Rule, ...] = (
    _rule(
        FLAG_DEFECT,
        [
            r"(?:teil)?defekt(?:e|er|es|en|em)?",
            r"kaputt(?:e|er|es|en|em)?",
            r"bastler\w*",
            r"bastel(?:projekt|objekt|ware)",
            r"ohne funktion",
            r"funktionslos",
            r"geht nicht (?:mehr )?an",
            r"(?:startet|bootet|zuendet) nicht(?: mehr)?",
            r"(?:funktioniert|funktionieren|funktionierte) nicht(?: mehr| richtig)?",
            r"laesst sich nicht (?:mehr )?(?:einschalten|anschalten|starten|laden)",
            r"kein (?:bild|signal)",
            r"(?:wasser|sturz|feuchtigkeits|display|bildschirm)schaden",
            r"(?:display|bildschirm|screen|glas|scheibe) " + _GAP2
            + r"(?:gebrochen|gesprungen|zersprungen|gerissen|zerbrochen|broken|cracked)",
            r"(?:display|bildschirm)(?:bruch|riss|sprung)",
            r"(?:riss|risse|sprung|spruenge) im (?:display|bildschirm|glas)",
            r"wackelkontakt",
            r"reparaturbeduerftig",
            r"(?:muss|sollte|muesste) repariert werden",
            r"als ersatzteil\w*",
            r"ersatzteil(?:spender|lager)",
            r"for parts",
            r"not working",
            r"broken",
            r"faulty",
            r"defective",
        ],
        exclude_near=_PACKAGING,  # "Karton leicht kaputt" is not a defect
        near_after=False,
    ),
    _rule(
        FLAG_BOX_ONLY,
        [
            r"nur (?:die |der |das |den )?(?:original ?)?(?:karton|ovp|verpackung|box|schachtel|"
            r"packung|originalverpackung|originalkarton|leerkarton)",
            r"leer(?:e|er|es|en)? (?:original ?)?(?:ovp|karton|verpackung|box|schachtel|"
            r"originalverpackung|originalkarton)",
            r"leerkarton",
            r"(?:box|karton|ovp|verpackung) ohne (?:inhalt|geraet|handy|konsole)",
            r"box only",
            r"empty box",
            r"only (?:the )?box",
        ],
        post_ok="fehlt fehlen hat weist zeigt leicht etwas beschaedigt leider minimal "
        "eingedrueckt eingerissen gebrauchsspuren",
    ),
    _rule(
        FLAG_LOCKED,
        [
            r"icloud (?:gesperrt|lock|locked|sperre|aktiv|aktiviert|verknuepft|gebunden|verbunden|drauf|drin)",
            r"icloud\w*(?:sperre|lock|locked)",
            r"aktivierungssperre",
            r"activation lock",
            r"gesperrt(?:e|er|es|en)?",
            r"frp(?: lock| sperre)?",
            r"(?:google|account|konto) (?:lock|sperre)",
            r"(?:passwort|kennwort|code|pin|passcode|sperrcode|entsperrcode) vergessen",
            r"blacklist\w*",
            r"mdm",
            r"locked",
        ],
    ),
    _rule(
        FLAG_FAKE,
        [
            r"replica",
            r"replika",
            r"replik",
            r"fake",
            r"faelschung",
            r"gefaelscht\w*",
            r"nachbau",
            r"imitat",
            r"plagiat",
            r"1 : 1 (?:kopie|replik\w*|replica|nachbau|qualitaet|rep|version|fake)",
        ],
    ),
    _rule(  # "Kopie der Rechnung" is not a fake
        FLAG_FAKE,
        [r"kopie"],
        exclude_near="rechnung beleg kaufbeleg quittung kassenbon bon kaufvertrag garantie "
        "garantiekarte ausweis papiere zertifikat dokument dokumente anleitung handbuch "
        "bedienungsanleitung backup datei dateien cd dvd",
    ),
    _rule(
        FLAG_SCAM,
        [
            r"vor(?:aus)?kasse",
            r"western union",
            r"moneygram",
            r"paysafe\w*",
            r"paypal (?:nur )?(?:an )?(?:freunde|friends|familie|family|f f|ff|fnf|fuf)",
            r"(?:freunde|friends) (?:(?:und|and|u) )?(?:familie|family)",
            r"(?:nur|ausschliesslich) (?:per |ueber |via |auf |mit )?"
            r"(?:whatsapp|whats app|telegram|sms|e mail|email|mail)",
            r"(?:bin|wohne|lebe|arbeite|zurzeit|derzeit|momentan|aktuell|beruflich) " + _GAP2 + r"im ausland",
            r"(?:versand|versende|verschicke) (?:nur )?(?:ins|aus dem|vom) ausland",
            r"(?:ueberweisung|zahlung|bezahlung) (?:vorab|im voraus)",
            r"(?:vorab|im voraus) (?:per )?(?:ueberweis\w*|bezahl\w*|zahl\w*)",
        ],
    ),
    _rule(
        FLAG_SWAP,
        [
            r"^tausche?",
            r"nur (?:gegen )?tausch\w*",
            r"tausche? (?:nur )?" + _GAP4 + r"gegen",
            r"(?:swap|trade) only",
            r"only (?:swap|trade)",
        ],
        exclude_near="oder auch gerne evtl eventuell ggf moeglich denkbar optional alternativ "
        "verkauf verkaufe",
    ),
    _rule(
        FLAG_RENT,
        [r"mietkauf", r"vermiet\w*", r"zur miete", r"zu mieten", r"mietgeraet"],
    ),
    _rule(  # financing offered as an *option* by a shop is fine
        FLAG_RENT,
        [r"ratenzahlung\w*", r"ratenkauf", r"finanzierung\w*", r"(?:auf|in) raten", r"leasing"],
        exclude_near="moeglich optional auch moeglichkeit anfrage angeboten alternativ oder gerne",
    ),
    _rule(
        FLAG_MISSING,
        [
            r"ohne (?:original ?)?(?:netzteil|ladegeraet|ladekabel|netzkabel|kabel|zubehoer|"
            r"controller|festplatte|ssd|hdd|akku|batterie|fernbedienung|ram|arbeitsspeicher|cpu|"
            r"prozessor|grafikkarte|gpu|lader|ladestation|dock|joycons|joy cons|stift|pen|"
            r"objektiv|speicherkarte|kuehler|luefter|mainboard|tastatur|maus)",
            r"(?:netzteil|ladegeraet|ladekabel|controller|akku|festplatte|fernbedienung|zubehoer|"
            r"kabel|ssd|hdd) (?:fehlt|fehlen|nicht dabei|nicht enthalten|nicht vorhanden|nicht inklusive)",
        ],
    ),
    _rule(FLAG_SIMLOCK, [r"(?:sim|net|netz) ?(?:lock|sperre)", r"providersperre"]),
    _rule(
        FLAG_UNTESTED,
        [
            r"ungetestet",
            r"nicht getestet",
            r"nicht (?:ueber)?prueft",
            r"untested",
            r"not tested",
            r"konnte " + _GAP2 + r"nicht (?:getestet|testen|pruefen|geprueft|ausprobieren|ausprobiert)",
            r"funktion (?:ist )?(?:unbekannt|nicht bekannt|ungeprueft)",
        ],
    ),
    _rule(FLAG_WHATSAPP, [r"whats ?app", r"wa nummer"]),
    _rule(FLAG_RESERVED, [r"reserviert"]),
)


def _flag_text(text: str) -> str:
    """Tokens (words + punctuation marks) separated by single spaces."""
    t = _prepare(text).replace("\n", " . ")
    return " ".join(re.findall(r"[^\W_]+|[:?!.,;]", t))


def _negated_before(prev: list[str]) -> bool:
    words = 0
    for tok in reversed(prev):
        if tok in _PUNCT or tok in _STOP_BEFORE:
            return False
        if tok in _NEG_BEFORE:
            return True
        if tok in _CONNECTORS:
            continue
        words += 1
        if words >= 3:
            return False
    return False


def _negated_after(nxt: list[str]) -> bool:
    if not nxt:
        return False
    if nxt[0] in _NEG_AFTER_DIRECT:
        return True
    if nxt[0] in (":", "?") and len(nxt) > 1 and nxt[1] in _NEG_AFTER_COLON:
        return True
    return nxt[0] == "nicht" and len(nxt) > 1 and nxt[1] in _NEG_AFTER_PHRASE


def _clause(tokens: list[str], limit: int) -> list[str]:
    """First `limit` tokens up to the next punctuation mark."""
    out: list[str] = []
    for tok in tokens:
        if tok in _PUNCT or len(out) >= limit:
            break
        out.append(tok)
    return out


def _rule_hits(rule: _Rule, s: str) -> bool:
    for pattern in rule.compiled:
        for m in pattern.finditer(s):
            prev = s[: m.start()].split()
            nxt = s[m.end() :].split()
            if _negated_before(prev) or _negated_after(nxt[:3]):
                continue
            if rule.post_ok and set(_clause(nxt, 2)) & rule.post_ok:
                continue
            if rule.exclude_near:
                context = _clause(list(reversed(prev)), 3)
                if rule.near_after:
                    context += _clause(nxt, 4)
                if rule.exclude_near.intersection(context):
                    continue
            return True
    return False


def detect_red_flags(text: str) -> list[str]:
    """Russian labels for warning signs in (German/English) ad text. The first line is
    treated as the title for the wanted-ad check. Negations ("nicht defekt",
    "keine Defekte", "ohne Simlock", "kein Fake") don't trigger."""
    if not text or not text.strip():
        return []
    flags: list[str] = []
    first_line = text.strip().split("\n", 1)[0]
    if is_wanted_ad(first_line):
        flags.append(FLAG_WANTED)
    s = _flag_text(text)
    for rule in _RULES:
        if rule.label not in flags and _rule_hits(rule, s):
            flags.append(rule.label)
    return flags


# ---------------------------------------------------------------------------
# Keywords
# ---------------------------------------------------------------------------


_NEGATION_WORDS = frozenset({
    "kein", "keine", "keinen", "keinem", "keiner", "keines", "nicht", "nie", "niemals", "ohne",
    "no", "not", "never", "without",
})


def _keyword_positions(norm: str, keyword: str) -> list[int]:
    """Where `keyword` starts a word ("tausch" matches "tausche", not "umtausch")."""
    return [m.start() for m in re.finditer(r"(?<![a-z0-9])" + re.escape(keyword), norm)]


def _negated(norm: str, pos: int) -> bool:
    """'kein Mining', 'nie für Mining genutzt', 'keine Defekte' — the keyword is denied."""
    return any(w in _NEGATION_WORDS for w in norm[:pos].split()[-3:])


def matches_keywords(text: str, include: list[str], exclude: list[str]) -> bool:
    """Case/umlaut-insensitive, word-start matching. include=[] means no requirement.
    An exclude word right after a negation ("kein Umtausch", "nie für Mining") does not count."""
    norm = normalize(text)
    inc = [k for k in (normalize(x) for x in include or []) if k]
    exc = [k for k in (normalize(x) for x in exclude or []) if k]
    for k in exc:
        if any(not _negated(norm, pos) for pos in _keyword_positions(norm, k)):
            return False
    return not inc or any(_keyword_positions(norm, k) for k in inc)


def matched_exclude_keyword(text: str, exclude: list[str]) -> str | None:
    """Which exclude keyword killed the ad (for the reason shown to the user)."""
    norm = normalize(text)
    for raw in exclude or []:
        k = normalize(raw)
        if k and any(not _negated(norm, pos) for pos in _keyword_positions(norm, k)):
            return raw
    return None
