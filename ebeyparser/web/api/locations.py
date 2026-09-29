"""Built-in places for the onboarding "where do you live?" field: ~340 German cities (all with
more than ~25 000 inhabitants that matter for classifieds, largest first) and the districts /
neighbourhoods of Berlin. No network. Search is by prefix, per word, by postal code, with
umlaut folding ("Muenchen", "Munchen"), a few English / Russian names and 1–2 typos.

Population is approximate (thousands) and only used for ranking. `value` is what goes into
SearchConfig.location: the city name, or the postal code where the name is ambiguous
("Halle (Saale)") and for Berlin districts (Kleinanzeigen resolves any PLZ).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

STATES = {
    "BE": "Berlin", "HH": "Hamburg", "BY": "Bayern", "NW": "Nordrhein-Westfalen", "HE": "Hessen",
    "BW": "Baden-Württemberg", "SN": "Sachsen", "NI": "Niedersachsen", "HB": "Bremen", "TH": "Thüringen",
    "ST": "Sachsen-Anhalt", "SH": "Schleswig-Holstein", "MV": "Mecklenburg-Vorpommern", "BB": "Brandenburg",
    "RP": "Rheinland-Pfalz", "SL": "Saarland",
}

# name|state|main postal code|population in thousands (approximate)
_CITIES = """
Berlin|BE|10115|3755
Hamburg|HH|20095|1892
München|BY|80331|1512
Köln|NW|50667|1084
Frankfurt am Main|HE|60311|773
Stuttgart|BW|70173|633
Düsseldorf|NW|40213|629
Leipzig|SN|04109|616
Dortmund|NW|44135|595
Essen|NW|45127|584
Bremen|HB|28195|577
Dresden|SN|01067|566
Hannover|NI|30159|548
Nürnberg|BY|90402|526
Duisburg|NW|47051|503
Bochum|NW|44787|365
Wuppertal|NW|42103|358
Bielefeld|NW|33602|338
Bonn|NW|53111|336
Münster|NW|48143|320
Mannheim|BW|68159|316
Karlsruhe|BW|76133|309
Augsburg|BY|86150|304
Wiesbaden|HE|65183|283
Mönchengladbach|NW|41061|268
Gelsenkirchen|NW|45879|264
Aachen|NW|52062|252
Braunschweig|NI|38100|251
Chemnitz|SN|09111|248
Kiel|SH|24103|247
Halle (Saale)|ST|06108|242
Magdeburg|ST|39104|240
Freiburg im Breisgau|BW|79098|237
Krefeld|NW|47798|228
Mainz|RP|55116|220
Lübeck|SH|23552|218
Erfurt|TH|99084|215
Oberhausen|NW|46045|210
Rostock|MV|18055|209
Kassel|HE|34117|204
Hagen|NW|58095|190
Potsdam|BB|14467|185
Saarbrücken|SL|66111|180
Hamm|NW|59065|180
Ludwigshafen am Rhein|RP|67059|174
Oldenburg|NI|26122|171
Mülheim an der Ruhr|NW|45468|171
Osnabrück|NI|49074|166
Leverkusen|NW|51373|166
Darmstadt|HE|64283|162
Heidelberg|BW|69117|160
Solingen|NW|42651|160
Herne|NW|44623|157
Regensburg|BY|93047|157
Neuss|NW|41460|155
Paderborn|NW|33098|153
Ingolstadt|BY|85049|142
Offenbach am Main|HE|63065|133
Fürth|BY|90762|131
Ulm|BW|89073|128
Heilbronn|BW|74072|128
Würzburg|BY|97070|127
Pforzheim|BW|75175|127
Wolfsburg|NI|38440|125
Göttingen|NI|37073|118
Bottrop|NW|46236|117
Reutlingen|BW|72764|117
Erlangen|BY|91052|116
Bremerhaven|HB|27568|115
Koblenz|RP|56068|114
Bergisch Gladbach|NW|51465|112
Remscheid|NW|42853|112
Trier|RP|54290|111
Recklinghausen|NW|45657|110
Jena|TH|07743|110
Moers|NW|47441|104
Salzgitter|NI|38226|104
Gütersloh|NW|33330|102
Siegen|NW|57072|101
Hildesheim|NI|31134|101
Hanau|HE|63450|101
Kaiserslautern|RP|67655|100
Cottbus|BB|03046|98
Schwerin|MV|19053|98
Witten|NW|58452|96
Esslingen am Neckar|BW|73728|94
Ludwigsburg|BW|71634|94
Gera|TH|07545|93
Iserlohn|NW|58636|92
Düren|NW|52349|92
Flensburg|SH|24937|92
Gießen|HE|35390|92
Tübingen|BW|72070|91
Zwickau|SN|08056|87
Ratingen|NW|40878|87
Lünen|NW|44532|86
Villingen-Schwenningen|BW|78050|86
Konstanz|BW|78462|85
Marl|NW|45768|85
Worms|RP|67547|84
Velbert|NW|42551|82
Minden|NW|32423|82
Neumünster|SH|24534|80
Norderstedt|SH|22846|80
Delmenhorst|NI|27749|79
Bamberg|BY|96047|79
Lüneburg|NI|21335|78
Marburg|HE|35037|77
Viersen|NW|41747|77
Rheine|NW|48431|77
Wilhelmshaven|NI|26382|76
Gladbeck|NW|45964|76
Dessau-Roßlau|ST|06844|75
Dorsten|NW|46282|75
Troisdorf|NW|53840|75
Landshut|BY|84028|75
Detmold|NW|32756|74
Castrop-Rauxel|NW|44575|74
Bayreuth|BY|95444|74
Arnsberg|NW|59821|73
Brandenburg an der Havel|BB|14776|73
Lüdenscheid|NW|58507|72
Aschaffenburg|BY|63739|72
Bocholt|NW|46395|71
Celle|NI|29221|70
Kempten (Allgäu)|BY|87435|70
Fulda|HE|36037|70
Lippstadt|NW|59555|68
Aalen|BW|73430|68
Dinslaken|NW|46535|68
Herford|NW|32052|67
Kerpen|NW|50171|67
Rüsselsheim am Main|HE|65428|66
Weimar|TH|99423|65
Sindelfingen|BW|71063|65
Neuwied|RP|56564|65
Dormagen|NW|41539|64
Grevenbroich|NW|41515|64
Rosenheim|BY|83022|64
Plauen|SN|08523|63
Neubrandenburg|MV|17033|63
Herten|NW|45699|62
Bergheim|NW|50126|62
Schwäbisch Gmünd|BW|73525|61
Garbsen|NI|30823|61
Friedrichshafen|BW|88045|61
Wesel|NW|46483|60
Offenburg|BW|77652|60
Neu-Ulm|BY|89231|60
Hürth|NW|50354|59
Stralsund|MV|18439|59
Greifswald|MV|17489|59
Langenfeld (Rheinland)|NW|40764|59
Unna|NW|59423|59
Euskirchen|NW|53879|58
Göppingen|BW|73033|58
Frankfurt (Oder)|BB|15230|57
Hameln|NI|31785|57
Stolberg (Rheinland)|NW|52222|56
Eschweiler|NW|52249|56
Sankt Augustin|NW|53757|56
Meerbusch|NW|40667|56
Görlitz|SN|02826|56
Waiblingen|BW|71332|56
Lingen (Ems)|NI|49808|56
Langenhagen|NI|30853|56
Baden-Baden|BW|76530|55
Hilden|NW|40721|55
Hattingen|NW|45525|54
Pulheim|NW|50259|54
Bad Salzuflen|NW|32105|54
Nordhorn|NI|48529|54
Schweinfurt|BY|97421|54
Bad Homburg vor der Höhe|HE|61348|54
Wetzlar|HE|35578|53
Ahlen|NW|59227|53
Passau|BY|94032|53
Kleve|NW|47533|53
Neustadt an der Weinstraße|RP|67433|53
Frechen|NW|50226|52
Menden (Sauerland)|NW|58706|52
Ibbenbüren|NW|49477|52
Wolfenbüttel|NI|38300|52
Böblingen|BW|71032|51
Gummersbach|NW|51643|51
Speyer|RP|67346|51
Bad Kreuznach|RP|55543|51
Ravensburg|BW|88212|51
Emden|NI|26721|50
Peine|NI|31224|50
Rastatt|BW|76437|50
Elmshorn|SH|25335|50
Erftstadt|NW|50374|50
Goslar|NI|38640|50
Willich|NW|47877|50
Heidenheim an der Brenz|BW|89518|50
Freising|BY|85354|49
Lörrach|BW|79539|49
Bergkamen|NW|59192|49
Gronau (Westf.)|NW|48599|49
Frankenthal (Pfalz)|RP|67227|49
Bad Oeynhausen|NW|32545|49
Rheda-Wiedenbrück|NW|33378|49
Leonberg|BW|71229|49
Cuxhaven|NI|27472|48
Straubing|BY|94315|48
Dachau|BY|85221|48
Soest|NW|59494|48
Singen (Hohentwiel)|BW|78224|48
Hennef (Sieg)|NW|53773|48
Bornheim|NW|53332|48
Oranienburg|BB|16515|48
Alsdorf|NW|52477|47
Melle|NI|49324|47
Stade|NI|21682|47
Landau in der Pfalz|RP|76829|47
Dülmen|NW|48249|47
Lahr/Schwarzwald|BW|77933|47
Oberursel (Taunus)|HE|61440|46
Schwerte|NW|58239|46
Filderstadt|BW|70794|46
Herzogenrath|NW|52134|46
Neunkirchen|SL|66538|46
Rodgau|HE|63110|46
Albstadt|BW|72458|46
Fellbach|BW|70734|45
Kaufbeuren|BY|87600|45
Bünde|NW|32257|45
Weinheim|BW|69469|45
Gotha|TH|99867|45
Falkensee|BB|14612|45
Lutherstadt Wittenberg|ST|06886|45
Lehrte|NI|31275|45
Memmingen|BY|87700|44
Pinneberg|SH|25421|44
Bruchsal|BW|76646|44
Erkrath|NW|40699|44
Rottenburg am Neckar|BW|72108|44
Neustadt am Rübenberge|NI|31535|44
Brühl|NW|50321|44
Kamen|NW|59174|43
Weiden in der Oberpfalz|BY|92637|43
Wismar|MV|23966|43
Bietigheim-Bissingen|BW|74321|43
Seevetal|NI|21217|43
Borken|NW|46325|43
Kaarst|NW|41564|43
Monheim am Rhein|NW|40789|43
Heinsberg|NW|52525|42
Dreieich|HE|63303|42
Eisenach|TH|99817|42
Siegburg|NW|53721|42
Wunstorf|NI|31515|42
Aurich|NI|26603|42
Homburg|SL|66424|42
Gifhorn|NI|38518|42
Nettetal|NW|41334|42
Amberg|BY|92224|42
Ansbach|BY|91522|42
Laatzen|NI|30880|41
Nordhausen|TH|99734|41
Kirchheim unter Teck|BW|73230|41
Nürtingen|BW|72622|41
Königswinter|NW|53639|41
Bensheim|HE|64625|41
Schwäbisch Hall|BW|74523|41
Bernau bei Berlin|BB|16321|41
Germering|BY|82110|41
Neumarkt in der Oberpfalz|BY|92318|41
Coburg|BY|96450|41
Schwabach|BY|91126|41
Lemgo|NW|32657|41
Freiberg|SN|09599|40
Halberstadt|ST|38820|40
Eberswalde|BB|16225|40
Weißenfels|ST|06667|40
Ettlingen|BW|76275|40
Schorndorf|BW|73614|40
Leinfelden-Echterdingen|BW|70771|40
Buxtehude|NI|21614|40
Buchholz in der Nordheide|NI|21244|40
Hofheim am Taunus|HE|65719|40
Pirmasens|RP|66953|40
Ahaus|NW|48683|40
Löhne|NW|32584|40
Stendal|ST|39576|39
Maintal|HE|63477|39
Neu-Isenburg|HE|63263|39
Ostfildern|BW|73760|39
Mettmann|NW|40822|39
Freital|SN|01705|39
Würselen|NW|52146|39
Völklingen|SL|66333|39
Königs Wusterhausen|BB|15711|39
Pirna|SN|01796|38
Bautzen|SN|02625|38
Langen (Hessen)|HE|63225|38
Papenburg|NI|26871|38
Kamp-Lintfort|NW|47475|38
Haltern am See|NW|45721|38
Ilmenau|TH|98693|38
Niederkassel|NW|53859|38
Erding|BY|85435|37
Fürstenfeldbruck|BY|82256|37
Cloppenburg|NI|49661|37
Wesseling|NW|50389|37
Greven|NW|48268|37
Warendorf|NW|48231|37
Beckum|NW|59269|37
Bitterfeld-Wolfen|ST|06766|37
Kehl|BW|77694|37
Backnang|BW|71522|37
Tuttlingen|BW|78532|36
Emsdetten|NW|48282|36
Coesfeld|NW|48653|36
Kempen|NW|47906|36
Suhl|TH|98527|36
Porta Westfalica|NW|32457|36
Mühlhausen/Thüringen|TH|99974|36
Limburg an der Lahn|HE|65549|36
Meppen|NI|49716|35
Winsen (Luhe)|NI|21423|35
Leer (Ostfriesland)|NI|26789|35
Datteln|NW|45711|35
Ingelheim am Rhein|RP|55218|35
Sankt Ingbert|SL|66386|35
Wermelskirchen|NW|42929|35
Balingen|BW|72336|35
Zweibrücken|RP|66482|34
Saarlouis|SL|66740|34
Merseburg|ST|06217|34
Radebeul|SN|01445|34
Wedel|SH|22880|34
Ahrensburg|SH|22926|34
Hemer|NW|58675|34
Seelze|NI|30926|34
Kornwestheim|BW|70806|34
Vechta|NI|49377|33
Jülich|NW|52428|33
Stuhr|NI|28816|33
Biberach an der Riß|BW|88400|33
Deggendorf|BY|94469|34
Bernburg (Saale)|ST|06406|32
Naumburg (Saale)|ST|06618|32
Wernigerode|ST|38855|32
Itzehoe|SH|25524|32
Achim|NI|28832|32
Georgsmarienhütte|NI|49124|32
Forchheim|BY|91301|32
Hoyerswerda|SN|02977|31
Altenburg|TH|04600|31
Geesthacht|SH|21502|31
Gevelsberg|NW|58285|31
Weil am Rhein|BW|79576|31
Nienburg/Weser|NI|31582|31
Werne|NW|59368|30
Riesa|SN|01587|30
Landsberg am Lech|BY|86899|30
Schönebeck (Elbe)|ST|39218|30
Güstrow|MV|18273|29
Meißen|SN|01662|28
Grimma|SN|04668|28
Rendsburg|SH|24768|28
Reinbek|SH|21465|28
Henstedt-Ulzburg|SH|24558|28
Arnstadt|TH|99310|28
Saalfeld/Saale|TH|07318|28
Werder (Havel)|BB|14542|28
Ludwigsfelde|BB|14974|28
Teltow|BB|14513|28
Hennigsdorf|BB|16761|27
Strausberg|BB|15344|27
Fürstenwalde/Spree|BB|15517|33
Blankenfelde-Mahlow|BB|15827|30
Schwedt/Oder|BB|16303|30
Neuruppin|BB|16816|31
Markkleeberg|SN|04416|25
Delitzsch|SN|04509|25
Zittau|SN|02763|25
Schleswig|SH|24837|25
Bad Oldesloe|SH|23843|25
Rathenow|BB|14712|24
Husum|SH|25813|23
Senftenberg|BB|01968|23
Kleinmachnow|BB|14532|21
Waren (Müritz)|MV|17192|21
"""

# Berlin districts (Bezirke) and well-known neighbourhoods (Ortsteile): name|postal code
_BERLIN = """
Mitte|10115
Friedrichshain-Kreuzberg|10997
Pankow|13187
Charlottenburg-Wilmersdorf|10585
Spandau|13597
Steglitz-Zehlendorf|12163
Tempelhof-Schöneberg|12099
Neukölln|12043
Treptow-Köpenick|12555
Marzahn-Hellersdorf|12679
Lichtenberg|10365
Reinickendorf|13403
Kreuzberg|10997
Friedrichshain|10243
Prenzlauer Berg|10405
Wedding|13347
Gesundbrunnen|13357
Moabit|10551
Tiergarten|10785
Hansaviertel|10557
Charlottenburg|10585
Wilmersdorf|10707
Westend|14050
Halensee|10711
Schmargendorf|14199
Grunewald|14193
Schöneberg|10827
Friedenau|12159
Tempelhof|12099
Mariendorf|12105
Marienfelde|12277
Lichtenrade|12305
Steglitz|12163
Lichterfelde|12203
Lankwitz|12247
Zehlendorf|14163
Dahlem|14195
Wannsee|14109
Staaken|13591
Kladow|14089
Siemensstadt|13629
Tegel|13507
Wittenau|13437
Märkisches Viertel|13439
Hermsdorf|13467
Frohnau|13465
Heiligensee|13503
Weißensee|13086
Heinersdorf|13089
Niederschönhausen|13156
Französisch Buchholz|13127
Buch|13125
Karow|13125
Hohenschönhausen|13053
Friedrichsfelde|10315
Karlshorst|10318
Rummelsburg|10317
Marzahn|12679
Hellersdorf|12627
Biesdorf|12683
Kaulsdorf|12621
Mahlsdorf|12623
Köpenick|12555
Alt-Treptow|12435
Plänterwald|12435
Baumschulenweg|12437
Johannisthal|12487
Adlershof|12489
Oberschöneweide|12459
Niederschöneweide|12439
Friedrichshagen|12587
Grünau|12527
Altglienicke|12524
Britz|12347
Buckow|12349
Rudow|12355
Gropiusstadt|12353
"""

# other spellings -> official name
_ALIASES = {
    "Munich": "München", "Cologne": "Köln", "Nuremberg": "Nürnberg", "Hanover": "Hannover",
    "Brunswick": "Braunschweig", "Frankfurt": "Frankfurt am Main", "Freiburg": "Freiburg im Breisgau",
    "Halle": "Halle (Saale)", "Ludwigshafen": "Ludwigshafen am Rhein", "Mülheim": "Mülheim an der Ruhr",
    "Offenbach": "Offenbach am Main", "Wittenberg": "Lutherstadt Wittenberg", "Kempten": "Kempten (Allgäu)",
    "Brandenburg": "Brandenburg an der Havel", "Bad Homburg": "Bad Homburg vor der Höhe",
    "Берлин": "Berlin", "Гамбург": "Hamburg", "Мюнхен": "München", "Кёльн": "Köln", "Кельн": "Köln",
    "Франкфурт": "Frankfurt am Main", "Штутгарт": "Stuttgart", "Дюссельдорф": "Düsseldorf",
    "Лейпциг": "Leipzig", "Дортмунд": "Dortmund", "Эссен": "Essen", "Бремен": "Bremen", "Дрезден": "Dresden",
    "Ганновер": "Hannover", "Нюрнберг": "Nürnberg", "Дуйсбург": "Duisburg", "Бохум": "Bochum",
    "Вупперталь": "Wuppertal", "Билефельд": "Bielefeld", "Бонн": "Bonn", "Мюнстер": "Münster",
    "Мангейм": "Mannheim", "Мангейм-на-Рейне": "Mannheim", "Карлсруэ": "Karlsruhe", "Аугсбург": "Augsburg",
    "Висбаден": "Wiesbaden", "Ахен": "Aachen", "Брауншвейг": "Braunschweig", "Киль": "Kiel",
    "Хемниц": "Chemnitz", "Галле": "Halle (Saale)", "Магдебург": "Magdeburg",
    "Фрайбург": "Freiburg im Breisgau", "Майнц": "Mainz", "Любек": "Lübeck", "Эрфурт": "Erfurt",
    "Росток": "Rostock", "Кассель": "Kassel", "Потсдам": "Potsdam", "Саарбрюккен": "Saarbrücken",
    "Гейдельберг": "Heidelberg", "Хайдельберг": "Heidelberg", "Регенсбург": "Regensburg",
    "Вюрцбург": "Würzburg", "Ульм": "Ulm", "Вольфсбург": "Wolfsburg", "Гёттинген": "Göttingen",
    "Йена": "Jena", "Ингольштадт": "Ingolstadt", "Оснабрюк": "Osnabrück", "Ольденбург": "Oldenburg",
    "Дармштадт": "Darmstadt", "Кобленц": "Koblenz", "Трир": "Trier", "Шверин": "Schwerin",
    "Коттбус": "Cottbus", "Веймар": "Weimar", "Бамберг": "Bamberg", "Пассау": "Passau",
    "Констанц": "Konstanz", "Тюбинген": "Tübingen", "Эрланген": "Erlangen", "Фюрт": "Fürth",
}


def _fold(text: str, umlaut: str = "ae") -> str:
    """Lower-case, umlauts as 'ae' (umlaut='ae') or 'a' (umlaut='a'), no accents or punctuation."""
    text = (text or "").lower().replace("ё", "е").replace("ß", "ss")
    if umlaut == "ae":
        text = text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w]+", " ", text)
    return " ".join(text.split())


@dataclass
class Place:
    name: str
    kind: str  # "city" | "district"
    state_code: str
    plz: str
    population: int  # thousands, approximate (districts: 0)
    parent: str = ""
    keys: list[str] = field(default_factory=list)  # folded names (official + aliases)

    @property
    def value(self) -> str:
        if self.kind != "city" or "(" in self.name or "/" in self.name:
            return self.plz
        return self.name

    @property
    def label(self) -> str:
        if self.kind == "district":
            return f"{self.name}, {self.parent}"
        return f"{self.name}, {STATES.get(self.state_code, self.state_code)}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": f"{self.kind}:{self.plz}:{_fold(self.name, 'a').replace(' ', '-')}",
            "name": self.name,
            "label": self.label,
            "value": self.value,
            "kind": self.kind,
            "state": STATES.get(self.state_code, self.state_code),
            "state_code": self.state_code,
            "plz": self.plz,
            "parent": self.parent or None,
            "population": self.population * 1000 if self.population else None,
        }


@lru_cache(maxsize=1)
def places() -> tuple[Place, ...]:
    out: list[Place] = []
    aliases: dict[str, list[str]] = {}
    for alias, official in _ALIASES.items():
        aliases.setdefault(official, []).append(alias)
    for line in _CITIES.strip().splitlines():
        name, state, plz, pop = line.split("|")
        names = [name, *aliases.get(name, [])]
        out.append(Place(name, "city", state, plz, int(pop), keys=_keys(names)))
    for line in _BERLIN.strip().splitlines():
        name, plz = line.split("|")
        out.append(Place(name, "district", "BE", plz, 0, parent="Berlin",
                         keys=_keys([name, f"Berlin {name}"])))
    return tuple(out)


def _keys(names: list[str]) -> list[str]:
    keys: list[str] = []
    for name in names:
        for variant in (_fold(name, "ae"), _fold(name, "a")):
            if variant and variant not in keys:
                keys.append(variant)
    return keys


def _distance(a: str, b: str, limit: int) -> int:
    """Levenshtein distance, early exit above `limit`."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = i
        for j, cb in enumerate(b, 1):
            value = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            cur.append(value)
            best = min(best, value)
        if best > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def _rank(place: Place, query: str) -> float | None:
    """Lower is better; None = no match."""
    best: float | None = None
    for key in place.keys:
        if key == query:
            score = 0.0
        elif key.startswith(query):
            score = 1.0
        elif any(word.startswith(query) for word in key.split()):
            score = 2.0
        elif len(query) >= 4 and query in key:
            score = 3.0
        else:
            score = None
            if len(query) >= 4:
                limit = 1 if len(query) < 7 else 2
                candidates = {key[: len(query)], key, *key.split()}
                dist = min(_distance(query, c, limit) for c in candidates)
                if dist <= limit:
                    score = 4.0 + dist
        if score is not None and (best is None or score < best):
            best = score
    return best


def search_places(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Places matching `query` (name, alias or postal code prefix), best first. An unknown
    5-digit postal code is returned as is (kind "plz"): Kleinanzeigen resolves any PLZ."""
    raw = " ".join((query or "").split())
    limit = max(1, min(50, int(limit)))
    if not raw:
        return [p.as_dict() for p in places() if p.kind == "city"][:limit]
    if raw.isdigit():
        hits = sorted((p for p in places() if p.plz.startswith(raw)),
                      key=lambda p: (p.plz != raw, p.kind != "city", -p.population))
        out = [p.as_dict() for p in hits[:limit]]
        if len(raw) == 5 and not any(p["plz"] == raw for p in out):
            out.insert(0, {"id": f"plz:{raw}", "name": raw, "label": f"Почтовый индекс {raw}", "value": raw,
                           "kind": "plz", "state": None, "state_code": None, "plz": raw, "parent": None,
                           "population": None})
        return out[:limit]
    folded = [_fold(raw, "ae"), _fold(raw, "a")]
    scored: list[tuple[float, int, int, Place]] = []
    for place in places():
        ranks = [r for q in dict.fromkeys(folded) if q and (r := _rank(place, q)) is not None]
        if ranks:
            scored.append((min(ranks), place.kind != "city", -place.population, place))
    scored.sort(key=lambda t: t[:3])
    return [p.as_dict() for *_, p in scored[:limit]]


__all__ = ["STATES", "Place", "places", "search_places"]
