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
# Price history keys
# ---------------------------------------------------------------------------

# family words sellers often leave out ("Samsung S21", "Nvidia 3080"): useless as anchors
_OMITTED_FAMILY = frozenset({"galaxy", "geforce", "radeon", "core", "playstation", "series", "edition"})
_GLUED_RE = re.compile(r"^([a-z]{3,})(\d{2,})[a-z]*$")  # "iphone13" -> "iphone" + "13"


def _is_model_token(tok: str) -> bool:
    return any(c.isdigit() for c in tok) and not _UNIT_TOKEN_RE.fullmatch(tok)


def _unglued(tokens: list[str]) -> list[str]:
    """Split "iphone13" into "iphone", "13" (sellers write both ways)."""
    out: list[str] = []
    for tok in tokens:
        glued = _GLUED_RE.match(tok)
        out.extend(glued.groups() if glued else (tok,))
    return out


def price_point_words(title: str, key: str = "") -> set[str]:
    """Index words of a remembered price: the product key's words plus model numbers and
    product families from the whole title (a key keeps only its 5 strongest words)."""
    key_toks = normalize(key).split()
    words = set(key_toks) | set(_unglued(key_toks))
    for tok in normalize(title).split():
        if _is_model_token(tok):
            words.add(tok)
            words.update(_unglued([tok]))
        elif tok in _FAMILY:
            words.add(tok)
    return words


def history_anchor_words(key: str) -> list[str]:
    """One or two words every remembered price of this product must share — the most specific
    model number and the product line ('iphone 13 128gb' -> ['13', 'iphone']). A cheap index
    lookup; comparable_is_relevant() makes the real decision afterwards."""
    toks = _unglued(normalize(key).split())
    if not toks:
        return []
    anchors: list[str] = []
    models = [t for t in toks if _is_model_token(t)]
    if models:  # "m1"/"s21"/"3080" beat bare small numbers like "13" or "2"
        anchors.append(max(models, key=lambda t: (any(c.isalpha() for c in t), len(t))))
    family = next(
        (t for t in toks if t in _FAMILY and t not in _MODEL_SUFFIXES and t not in _OMITTED_FAMILY), None
    )
    if family and family not in anchors:
        anchors.append(family)
    if not anchors:
        rest = [t for t in toks if t not in _BRANDS and t not in _LOW_VALUE and t not in _COLORS
                and t not in _FILLER and not _UNIT_TOKEN_RE.fullmatch(t)]
        anchors.append(max(rest, key=len) if rest else toks[0])
    return anchors


# ---------------------------------------------------------------------------
# Wanted ads
# ---------------------------------------------------------------------------

_WANTED_FIRST = frozenset({"suche", "suchen", "sucht", "kaufe", "ankauf", "gesucht", "wtb", "wanted"})
_WANTED_ANYWHERE = frozenset({"gesucht", "ankauf", "wanted"})
_OFFER_FIRST = frozenset({"verkaufe", "verkauf", "biete", "tausche", "tausch"})


# "zahle gut", "brauche dringend", "wer verkauft ...?", "kaufe ... an": buyer phrasings in a title
_WANTED_TITLE_RE = re.compile(
    r"\bzahle (?:gut|gute|guten|fair|faire|top|sofort|bar|bis|mehr|hoechstpreis\w*)\b"
    r"|^(?:brauche|benoetige|wer verkauft|wer hat|biete bis)\b"
    r"|\b(?:suche|brauche|benoetige) dringend\b"
    r"|\b(?:kaufe|kaufen)\b(?:\s+\w+){0,5}\s+an\b"
)


def is_wanted_ad(title: str) -> bool:
    """True for "Suche ...", "Kaufe ...", "Ankauf", "... gesucht", "... – zahle gut",
    "Brauche dringend ...", "Wer verkauft ...?" — someone buying, not selling."""
    norm = normalize(title)
    tokens = norm.split()
    if not tokens:
        return False
    if tokens[0] in _WANTED_FIRST:
        return True
    if tokens[:2] in (["wir", "kaufen"], ["ich", "kaufe"], ["ich", "suche"], ["wir", "suchen"]):
        return True
    if _WANTED_ANYWHERE.intersection(tokens):
        return True
    if _WANTED_TITLE_RE.search(norm):
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
FLAG_WHATSAPP = "Просит связь через WhatsApp/Telegram"
FLAG_EMAIL = "Просит писать на e-mail"
FLAG_RESERVED = "Возможно, уже зарезервировано"
FLAG_DELETED = "Объявление удалено"
FLAG_TOO_GOOD = "Подозрительно дёшево и только пересылка — похоже на развод"
FLAG_BAIT = "Подозрительно дёшево и уводит в WhatsApp / Telegram / e-mail или на предоплату — похоже на развод"

SEVERE_FLAGS: frozenset[str] = frozenset(
    {FLAG_WANTED, FLAG_DEFECT, FLAG_BOX_ONLY, FLAG_LOCKED, FLAG_FAKE, FLAG_SCAM, FLAG_SWAP, FLAG_RENT,
     FLAG_DELETED, FLAG_TOO_GOOD, FLAG_BAIT}
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
# "keine Garantie oder Rücknahme bei Defekten" is the usual private-sale disclaimer, not a defect
_DISCLAIMER = (
    "garantie gewaehrleistung ruecknahme haftung umtausch sachmaengelhaftung garantieanspruch "
    # "für eventuelle / etwaige / spätere Defekte wird nicht gehaftet": hypothetical, not a report
    "eventuelle eventuellen eventuell evtl etwaige etwaigen moegliche moeglichen spaetere spaeteren "
    "kuenftige kuenftigen auftretende auftretenden"
)

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
            r"geht (?:seitdem |seit dem |leider |jetzt |seit \w+ )?nicht mehr",
            r"geht (?:ab und zu|manchmal|oefters?|staendig|immer wieder|einfach) (?:\w+ ){0,2}aus",
            r"schaltet sich (?:\w+ ){0,2}nicht (?:mehr )?(?:ein|an)",
            r"(?:startet|bootet|zuendet) nicht(?: mehr)?",
            r"(?:laedt|ladet) (?:\w+ )?nicht(?: mehr)?",
            r"(?:ladebuchse|ladeanschluss|ladeport|buchse|anschluss|akku|display|bildschirm|touch|touchscreen|"
            r"lautsprecher|mikrofon|kamera|mainboard|platine|luefter|motor) " + _GAP4 + r"(?:hinueber|im eimer|tot)",
            r"(?:ist|sind|scheint|wohl|vermutlich|leider) hinueber",
            r"defec?kt\w*",
            r"(?:face ?id|touch ?id|touch|touchscreen|kamera|mikrofon|lautsprecher|wlan|wifi|bluetooth|"
            r"homebutton|home button|lautstaerketaste) (?:geht|funktioniert|reagiert|klappt) (?:\w+ ){0,4}nicht",
            r"(?:bildschirm|display) bleibt (?:\w+ )?schwarz",
            r"bleibt (?:der |das )?(?:bildschirm|display) (?:\w+ )?schwarz",
            r"(?:akku|batterie) " + _GAP2 + r"(?:aufgeblaeht|aufgequollen|gebläht|blaeht)",
            r"(?:linie|linien|streifen|pixelfehler|flecken|fleck|schatten) im (?:display|bildschirm)",
            r"artefakt\w*",
            r"bildfehler\w*",
            r"gimbal ?fehler\w*",
            r"(?:autofokus|fokus|zoom|objektiv|gimbal) " + _GAP2 + r"spinnt",
            r"luefter " + _GAP2 + r"(?:schleift|rattert|klackert|klappert)",
            r"ueberhitz\w*",
            r"wird (?:zu|sehr|extrem|ziemlich|schnell) heiss",
            r"wasser(?:kontakt|einbruch)",
            r"(?:ins wasser|in die toilette|ins klo) gefallen",
            r"(?:funktioniert|funktionieren|funktionierte) nicht(?: mehr| richtig)?",
            r"laesst sich nicht (?:mehr )?(?:einschalten|anschalten|starten|laden)",
            r"kein (?:bild|signal)",
            r"(?:wasser|sturz|feuchtigkeits|display|bildschirm)schaden",
            r"(?:display|bildschirm|screen|glas|scheibe) " + _GAP2
            + r"(?:gebrochen|gesprungen|zersprungen|gerissen|zerbrochen|broken|cracked)",
            r"(?:display|bildschirm|screen|glas|scheibe|rueckseite|backcover|gehaeuse) " + _GAP4
            + r"(?:einen |einige |ein paar |mehrere )?(?:sprung|spruenge|riss|risse|haarriss\w*|bruch)",
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
        exclude_near=f"{_PACKAGING} {_DISCLAIMER}",  # "Karton leicht kaputt" is not a defect
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
            r"(?:ovp|karton|verpackung|box|schachtel|originalverpackung|originalkarton) (?:ist )?leer",
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
            r"i ?cloud " + _GAP2 + r"(?:gesp?er+t\w{0,2}|gespeert|lock|locked|sperre|aktiv|aktiviert|verknuepft|"
            r"gebunden|verbunden|drauf|drin|angemeldet|eingeloggt)",
            r"i ?cloud\w*(?:sperre|lock|locked)",
            r"aktivi?e?rungs? ?sperre\w*",
            r"(?:haengt|haenge|steckt|bleibt) (?:\w+ )?(?:in|bei|an) (?:der |dem )?aktivierung\w*",
            r"activation lock",
            r"gesp?er+t(?:e|er|es|en)?",
            r"frp(?: lock| sperre)?",
            r"(?:google|account|konto) (?:lock|sperre)",
            r"(?:google|samsung|xiaomi|huawei|mi) ?(?:konto|account|id) " + _GAP2
            + r"(?:gesp?er+t\w{0,2}|lock|locked|sperre|drauf|drin|angemeldet|eingeloggt|verknuepft|aktiv)",
            r"(?:apple ?id|apple konto|apple account|icloud konto|icloud account) " + _GAP4
            + r"(?:angemeldet|eingeloggt|drauf|drin|verknuepft|verbunden|gebunden|aktiv|unbekannt|vergessen|"
            r"gesp?er+t\w{0,2})",
            r"konto " + _GAP4 + r"(?:verbunden|verknuepft|angemeldet|eingeloggt|gebunden)",
            r"(?:passwort|kennwort|code|pin|passcode|sperrcode|entsperrcode|entsperrmuster|muster) " + _GAP4
            + r"(?:vergessen|unbekannt|nicht mehr (?:weiss|kenne|bekannt|weiß))",
            r"(?:passwort|kennwort|code|pin|passcode|sperrcode|entsperrcode) (?:(?:[^\W_]+|,) ){0,6}"
            r"(?:den|das|die) (?:ich )?(?:[^\W_]+ ){0,2}nicht mehr (?:weiss|kenne)",
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
            r"1 ?zu ?1",
            r"china ?(?:version|ware|kopie|nachbau)",
            r"chinaware",
            # "nicht original" — but "nicht original verpackt" is about the box
            r"(?:nicht|kein|keine) (?:\w+ )?original(?:e|er|es)?(?! verpack\w*| karton| ovp| zubehoer| kabel| ladekabel"
            r"| netzteil| ladegeraet| rechnung| papiere| box)",
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
            r"vor(?:r?aus)? ?kasse",
            r"western union",
            r"auf montage",
            r"moneygram",
            r"paysafe\w*",
            r"paypal (?:nur )?(?:an )?(?:freunde|friends|familie|family|f f|ff|fnf|fuf)",
            r"(?:freunde|friends) (?:(?:und|and|u) )?(?:familie|family)",
            r"(?:nur|ausschliesslich) (?:per |ueber |via |auf |mit )?"
            r"(?:whatsapp|whats app|telegram|sms|e mail|email|mail)",
            r"(?:bin|wohne|lebe|arbeite|zurzeit|derzeit|momentan|aktuell|beruflich) " + _GAP2 + r"im ausland",
            r"(?:versand|versende|verschicke) (?:nur )?(?:ins|aus dem|vom) ausland",
            r"(?:ueberweisung|zahlung|bezahlung) " + _GAP2 + r"(?:vorab|im voraus|vorraus)",
            r"(?:vorab|im voraus) (?:per )?(?:ueberweis\w*|bezahl\w*|zahl\w*)",
            # Kleinanzeigen's own "Sicher bezahlen" happens in the app: a link, an e-mail or a
            # messenger next to it is the phishing trick ("schick mir deine E-Mail, ich sende den Link")
            r"sicher bezahlen (?:[^\s.!?]+ ){0,10}(?:link\w*|whatsapp|whats app|telegram|formular\w*|"
            r"(?:deine|ihre|your) (?:e mail|email|mail)\w*)",
            r"(?:link\w*|whatsapp|whats app|telegram|formular\w*|(?:deine|ihre|your) (?:e mail|email|mail)\w*) "
            r"(?:[^\s.!?]+ ){0,10}sicher bezahlen",
            r"(?:zahlungs|bezahl|kauf|sicherheits)link\w*",
        ],
    ),
    _rule(
        FLAG_SWAP,
        [
            r"^tausche?",
            r"nur (?:gegen )?tausch\w*",
            r"tausche? (?:nur )?" + _GAP4 + r"(?:gegen|gg)",
            r"(?:swap|trade) only",
            r"only (?:swap|trade)",
        ],
        exclude_near="oder auch gerne evtl eventuell ggf moeglich denkbar optional alternativ "
        "verkauf verkaufe",
    ),
    _rule(  # swap-only, said the other way round
        FLAG_SWAP,
        [
            r"kein (?:geld|bargeld)",
            r"kein verkauf(?! an| ins| nach| unter| ueber)",
            r"verkauf (?:ist )?(?:nicht|leider nicht) (?:gewuenscht|moeglich|gewollt)",
            r"geld interessiert (?:mich )?(?:eher |leider |gar |ueberhaupt )?nicht",
        ],
    ),
    _rule(  # buyer phrasings anywhere in the text
        FLAG_WANTED,
        [
            r"zahle (?:gut|gute|guten|fair|faire|top|sofort|bar|bis|hoechstpreis\w*)",
            r"suche dringend",
            r"bin auf der suche nach",
            r"bitte alles anbieten",
            r"wir kaufen (?:dein|deine|deinen|ihr|ihre|ihren|alle|jede|jedes)",
            r"kaufe (?:dein|deine|deinen|ihr|ihre|ihren|alle|jede|jedes) (?:\w+ ){0,4}an",
        ],
    ),
    _rule(  # "Brauche dringend ein iPhone 13" — but "brauche dringend ein neues / Platz / Geld" is a seller
        FLAG_WANTED,
        [r"(?:brauche|benoetige) (?:dringend|unbedingt|sofort) (?:ein|eine|einen)"],
        exclude_near="neues neue neuen neuer groesseres groessere platz geld deshalb daher darum weil",
    ),
    _rule(
        FLAG_RENT,
        [r"mietkauf", r"vermiet\w*", r"zur miete", r"zu mieten", r"mietgeraet", r"verleih\w*",
         r"(?:preis|kosten|miete) (?:pro|je) (?:tag|woche|wochenende|monat)"],
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
            r"controller|festplatte|ssd|hdd|akku|akkus|batterie|fernbedienung|ram|arbeitsspeicher|cpu|"
            r"prozessor|grafikkarte|gpu|lader|ladestation|dock|joycons|joy cons|stift|pen|"
            r"objektiv|speicherkarte|kuehler|luefter|mainboard|tastatur|maus|ohrhoerer|ladecase|"
            r"ladeschale|basisstation|aufsaetze|saugrohr)",
            r"(?:netzteil|ladegeraet|ladekabel|controller|akku|akkus|festplatte|fernbedienung|zubehoer|"
            r"kabel|ssd|hdd|objektiv|dock|joy cons|joycons|ladecase|ohrhoerer|lader) (?:(?:und|u|sowie|\w+) ){0,3}"
            r"(?:ist |sind )?(?:fehlt|fehlen|nicht dabei|nicht enthalten|nicht vorhanden|nicht inklusive)",
            r"(?:akku|akkus|lader|ladegeraet|netzteil|controller|fernbedienung|objektiv|dock|zubehoer|"
            r"ohrhoerer) (?:(?:und|u|sowie|\w+) ){0,3}behalte ich",
            r"(?<!beats )(?<!bose )solo(?! (?:\d|pro|buds|bud|hd|ii|iii))",
            r"sologeraet\w*",
            r"nur (?:das |den |der )?grundgeraet",
            r"nur (?:der |den |das )?(?:body|gehaeuse|kamerabody)",
            r"body only",
            r"nur (?:das |den |der )?(?:ladecase|case|ladeschale)",
            r"nur (?:der |den |das )?(?:linke|linken|linker|rechte|rechten|rechter) "
            r"(?:ohrhoerer|airpod|kopfhoerer|earbud|stoepsel|controller|joy con)",
            r"(?:linker|rechter|linken|rechten) (?:ohrhoerer|airpod|earbud) " + _GAP4 + r"verloren",
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
    _rule(FLAG_WHATSAPP, [r"whats ?app", r"wa nummer", r"telegram\w*", r"tg nummer"]),
    _rule(  # moving the talk off the platform: the e-mail-only scam
        FLAG_EMAIL,
        [r"(?:schreib\w*|melde\w*|kontakt\w*|anfrage\w*|antwort\w*|nachricht\w*|schick\w*|write|contact) "
         r"(?:[^\s.!?]+ ){0,5}(?:e mail|email|mail)(?:adresse)?"],
    ),
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


# Pay first, goods later — normal for shops, a scam pattern with a too-good private offer.
_PREPAY_PATTERNS = [
    r"(?:versand|verschicke|versende|lieferung|paket) (?:\w+ ){0,3}(?:erst |nur |direkt |sofort |gleich )?nach "
    r"(?:dem |der )?(?:zahlungseingang|geldeingang|zahlung|bezahlung|ueberweisung|eingang der zahlung)",
    r"nach (?:dem )?(?:zahlungseingang|geldeingang|eingang der zahlung)",
    r"(?:zahlung|bezahlung|ueberweisung) (?:vor|vorab|vor dem) (?:versand|lieferung)",
    r"(?:bezahlung|zahlung) vorab",
]

# Seller won't meet: harmless on its own, a scam pattern together with a too-good price.
_REMOTE_ONLY_RULE = _rule(
    "remote-only",
    [
        r"nur (?:per |mit )?(?:versand|verschicken|post|dhl|hermes)",
        r"(?:keine|kein) (?:abholung|selbstabholung|abholer|besichtigung|treffen)",
        r"(?:abholung|selbstabholung|besichtigung) (?:ist )?(?:nicht|leider nicht) (?:moeglich|drin)",
        r"(?:versand|verkauf) (?:nur )?(?:gegen|per|ueber|via) paypal",
        r"nur (?:per |ueber |via |mit )?paypal",
        r"vor(?:r?aus)? ?kasse",
        r"only shipping",
        r"no pick ?up",
    ] + _PREPAY_PATTERNS,
)
_PREPAY_RULE = _rule("prepay", _PREPAY_PATTERNS)
_NEW_WORDS = frozenset(
    "neu neue neuer neues neuwertig unbenutzt ungenutzt ovp originalverpackt originalverpackung versiegelt "
    "sealed nagelneu brandneu ungeoeffnet".split()
)


def is_prepay_shipping(text: str) -> bool:
    """"Versand erfolgt direkt nach Zahlungseingang": the buyer pays before seeing anything."""
    if not text or not text.strip():
        return False
    return _rule_hits(_PREPAY_RULE, _flag_text(text))


def says_new(text: str) -> bool:
    """"Neu", "unbenutzt", "OVP versiegelt" — the usual bait of too-good offers."""
    return bool(_NEW_WORDS.intersection(normalize(text).split()))


def is_remote_only(text: str) -> bool:
    """"Nur Versand", "keine Abholung", "nur PayPal", "Vorkasse": the buyer can't see the item."""
    if not text or not text.strip():
        return False
    return _rule_hits(_REMOTE_ONLY_RULE, _flag_text(text))


def is_model_key(key: str) -> bool:
    """Does a product key name a concrete model ("iphone 13", "rtx 3080") rather than a kind
    of thing ("kinderwagen bugaboo")? Only such keys give prices precise enough to skip an ad
    without looking at it."""
    return any(_is_model_token(t) for t in _unglued(normalize(key).split()))


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
    if FLAG_EMAIL not in flags and _EMAIL_ADDRESS_RE.search(text):
        flags.append(FLAG_EMAIL)
    return flags


# "max.muster@gmail.com", "max (at) web.de", "max[at]gmx.de"
_EMAIL_ADDRESS_RE = re.compile(
    r"[\w.+-]+\s*(?:@|\(\s*(?:at|ät)\s*\)|\[\s*(?:at|ät)\s*\])\s*[\w-]+\s*(?:\.|\(\s*dot\s*\)|\[\s*dot\s*\])\s*[a-z]{2,6}\b",
    re.IGNORECASE,
)


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
