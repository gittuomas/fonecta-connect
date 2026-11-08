"""Deterministic parser for hours-related reviews in the Fonecta simulator (fi / sv / nb / en).

Standard library only, so the engine can import it directly:

    import sys; sys.path.insert(0, "<...>/analysis/scripts/02_text_signals")
    from review_parser import parse_review, review_events
    parse_review("Netissä luki auki 8–17, mutta pitkänäperjantaina oli suljettu.")
    -> {'events': [{'template': 'FI_SHOWN_OPEN_WAS_CLOSED', 'kind': 'CLOSED', 'day_key': 'GOOD_FRIDAY',
                    'shown_hours': '08:00-17:00', 'implied_truth': 'closed', ...}], 'flags': [...], ...}

Each review is split into sentences; every sentence is matched against a fixed template inventory
(TEMPLATES below). Holiday phrases are captured as free text and mapped to a day_key with HOLIDAYS
(inflected Finnish essives, Swedish 'på X', Norwegian bare forms, English 'on X').

Event kinds (what the reviewer says happened on a special day)
    CLOSED             listing said open (sometimes with the listed hours), place was closed
    CLOSED_EARLY       listing said H, place closed already at T   -> implied truth 'H.start-T'
    ARRIVED_CLOSED     came at T and doors were already closed      -> closes before T (or closed)
    CLOSED_COMPLAINT   'pity, closed on X' (no statement about the listing)
    WRONG_HOURS        'hours were wrong on X' (no hours given)
    OPEN_LISTED_CLOSED listing said closed, place was open
    OPEN_THANKS        'thanks for being open on X' (no statement about the listing)
Review-level flags (no day)
    CLOSED_FOR_GOOD, REGULAR_CHANGED (with optional parsed weekly hours), SEASONAL_CLOSED,
    GENERIC_HOURS (filler like 'Päivittäkää aukioloajat!', 'Wasted trip.')

Day keys that are generic ('on the holiday', 'på en helgdag') are returned as day_key=None,
day_generic=True. EVENT and SCHOOL_BREAK phrases map to 'EVENT' / 'SCHOOL_BREAK'.
"""
from __future__ import annotations

import re
import unicodedata

# --------------------------------------------------------------------------- holiday lexicon
# key: normalised phrase (lower case, leading preposition stripped) -> day_key
HOLIDAYS = {
    # ---- Finnish (essive / adessive forms used by the templates; nominatives added for robustness)
    "uudenvuodenpäivänä": "NEW_YEARS_DAY", "uudenvuodenpäivä": "NEW_YEARS_DAY",
    "loppiaisaattona": "EPIPHANY_EVE", "loppiaisaatto": "EPIPHANY_EVE",
    "loppiaisena": "EPIPHANY", "loppiainen": "EPIPHANY",
    "kiirastorstaina": "MAUNDY_THURSDAY", "kiirastorstai": "MAUNDY_THURSDAY",
    "pitkänäperjantaina": "GOOD_FRIDAY", "pitkäperjantaina": "GOOD_FRIDAY", "pitkäperjantai": "GOOD_FRIDAY",
    "pääsiäislauantaina": "EASTER_SATURDAY", "pääsiäislauantai": "EASTER_SATURDAY",
    "pääsiäispäivänä": "EASTER_SUNDAY", "pääsiäispäivä": "EASTER_SUNDAY",
    "toisena pääsiäispäivänä": "EASTER_MONDAY", "2. pääsiäispäivänä": "EASTER_MONDAY",
    "vappuaattona": "MAY_EVE", "vappuaatto": "MAY_EVE",
    "vappuna": "MAY_DAY", "vappu": "MAY_DAY", "vappupäivänä": "MAY_DAY",
    "helatorstaina": "ASCENSION", "helatorstai": "ASCENSION",
    "helluntaina": "PENTECOST", "helluntai": "PENTECOST",
    "äitienpäivänä": "MOTHERS_DAY", "äitienpäivä": "MOTHERS_DAY",
    "juhannusaattona": "MIDSUMMER_EVE", "juhannusaatto": "MIDSUMMER_EVE",
    "juhannuspäivänä": "MIDSUMMER_DAY", "juhannuspäivä": "MIDSUMMER_DAY",
    "pyhäinpäivänä": "ALL_SAINTS", "pyhäinpäivä": "ALL_SAINTS",
    "isänpäivänä": "FATHERS_DAY", "isänpäivä": "FATHERS_DAY",
    "black fridayna": "BLACK_FRIDAY",
    "itsenäisyyspäivänä": "INDEPENDENCE_DAY", "itsenäisyyspäivä": "INDEPENDENCE_DAY",
    "aatonaattona": "DEC23", "aatonaatto": "DEC23",
    "jouluaattona": "XMAS_EVE", "jouluaatto": "XMAS_EVE",
    "joulupäivänä": "XMAS_DAY", "joulupäivä": "XMAS_DAY",
    "tapaninpäivänä": "BOXING_DAY", "tapaninpäivä": "BOXING_DAY",
    "välipäivinä": "INTERDAY", "välipäivänä": "INTERDAY",
    "uudenvuodenaattona": "NYE", "uudenvuodenaatto": "NYE",
    "tapahtumailtana": "EVENT", "lomaviikolla": "SCHOOL_BREAK",
    # ---- Swedish
    "nyårsdagen": "NEW_YEARS_DAY", "trettondagsafton": "EPIPHANY_EVE", "trettondedagen": "EPIPHANY",
    "trettondedag jul": "EPIPHANY", "skärtorsdagen": "MAUNDY_THURSDAY", "långfredagen": "GOOD_FRIDAY",
    "påskafton": "EASTER_SATURDAY", "påskdagen": "EASTER_SUNDAY", "annandag påsk": "EASTER_MONDAY",
    "valborg": "MAY_EVE", "valborgsmässoafton": "MAY_EVE", "första maj": "MAY_DAY",
    "kristi himmelsfärd": "ASCENSION", "kristi himmelsfärdsdag": "ASCENSION",
    "pingstafton": "PENTECOST_EVE", "pingstdagen": "PENTECOST", "mors dag": "MOTHERS_DAY",
    "midsommarafton": "MIDSUMMER_EVE", "midsommardagen": "MIDSUMMER_DAY", "nationaldagen": "NATIONAL_DAY",
    "alla helgons dag": "ALL_SAINTS", "fars dag": "FATHERS_DAY", "black friday": "BLACK_FRIDAY",
    "lucia": "LUCIA", "dagen före julafton": "DEC23", "lillejulafton": "DEC23", "julafton": "XMAS_EVE",
    "juldagen": "XMAS_DAY", "annandag jul": "BOXING_DAY", "mellandagarna": "INTERDAY",
    "nyårsafton": "NYE", "evenemanget": "EVENT", "lovet": "SCHOOL_BREAK",
    # ---- Norwegian (bokmål)
    "første nyttårsdag": "NEW_YEARS_DAY", "nyttårsdag": "NEW_YEARS_DAY", "skjærtorsdag": "MAUNDY_THURSDAY",
    "langfredag": "GOOD_FRIDAY", "påskeaften": "EASTER_SATURDAY", "første påskedag": "EASTER_SUNDAY",
    "påskedag": "EASTER_SUNDAY", "andre påskedag": "EASTER_MONDAY", "1. mai": "MAY_DAY",
    "arbeidernes dag": "MAY_DAY", "17. mai": "NATIONAL_DAY", "grunnlovsdag": "NATIONAL_DAY",
    "kristi himmelfartsdag": "ASCENSION", "pinseaften": "PENTECOST_EVE", "første pinsedag": "PENTECOST",
    "pinsedag": "PENTECOST", "andre pinsedag": "WHIT_MONDAY", "morsdag": "MOTHERS_DAY", "farsdag": "FATHERS_DAY",
    "lille julaften": "DEC23", "julaften": "XMAS_EVE", "første juledag": "XMAS_DAY", "andre juledag": "BOXING_DAY",
    "romjulen": "INTERDAY", "romjul": "INTERDAY", "nyttårsaften": "NYE", "arrangementet": "EVENT",
    "ferien": "SCHOOL_BREAK", "skoleferien": "SCHOOL_BREAK",
    # ---- English
    "new year's day": "NEW_YEARS_DAY", "epiphany eve": "EPIPHANY_EVE", "epiphany": "EPIPHANY",
    "maundy thursday": "MAUNDY_THURSDAY", "good friday": "GOOD_FRIDAY", "easter saturday": "EASTER_SATURDAY",
    "easter sunday": "EASTER_SUNDAY", "easter monday": "EASTER_MONDAY", "may day eve": "MAY_EVE",
    "may day": "MAY_DAY", "ascension day": "ASCENSION", "pentecost": "PENTECOST", "whitsun eve": "PENTECOST_EVE",
    "whit monday": "WHIT_MONDAY", "mother's day": "MOTHERS_DAY", "midsummer eve": "MIDSUMMER_EVE",
    "midsummer day": "MIDSUMMER_DAY", "national day": "NATIONAL_DAY", "all saints' day": "ALL_SAINTS",
    "father's day": "FATHERS_DAY", "independence day": "INDEPENDENCE_DAY",
    "day before christmas eve": "DEC23", "christmas eve": "XMAS_EVE", "christmas day": "XMAS_DAY",
    "boxing day": "BOXING_DAY", "new year's eve": "NYE", "st lucy's day": "LUCIA",
    "the event": "EVENT", "the school break": "SCHOOL_BREAK", "the school holiday": "SCHOOL_BREAK",
}
GENERIC_DAY = {"the holiday", "en helgdag", "helgdag", "en helligdag", "pyhänä", "pyhäpäivänä"}
_PREP = re.compile(r"^(på|on|i|under|in)\s+", re.I)


def map_holiday(phrase: str):
    """Free-text holiday phrase -> (day_key or None, generic: bool)."""
    p = unicodedata.normalize("NFC", phrase).strip().strip(",.!").lower().replace("’", "'")
    p = re.sub(r"\s+", " ", p)
    if p in HOLIDAYS:
        return HOLIDAYS[p], False
    q = _PREP.sub("", p)
    if q in HOLIDAYS:
        return HOLIDAYS[q], False
    if p in GENERIC_DAY or q in GENERIC_DAY:
        return None, True
    return None, False


# --------------------------------------------------------------------------- hours helpers
_T = r"\d{1,2}(?:[.:]\d{2})?"
_IV = rf"{_T}\s*[–-]\s*{_T}"
_H = rf"{_IV}(?:\s*(?:,|ja|och|og|and)\s*{_IV})*"


def _hm(t: str) -> str:
    t = t.strip()
    if re.fullmatch(r"\d{1,2}", t):
        return f"{int(t):02d}:00"
    h, m = re.split(r"[.:]", t)
    return f"{int(h):02d}:{int(m):02d}"


def norm_hours(s: str | None) -> str | None:
    """'7.30–20' -> '07:30-20:00'; '10–14 ja 17–22' -> '10:00-14:00,17:00-22:00'; '12–1' -> '12:00-01:00'."""
    if not s:
        return None
    out = []
    for a, b in re.findall(rf"({_T})\s*[–-]\s*({_T})", s):
        a, b = _hm(a), _hm(b)
        if b == "24:00":
            b = "00:00"
        out.append(f"{a}-{b}")
    return ",".join(out) or None


def _to_min(hm: str) -> int:
    return int(hm[:2]) * 60 + int(hm[3:])


# --------------------------------------------------------------------------- templates
HOL = r"(?P<hol>.+?)"
SH = rf"(?P<shown>{_H})"
TT = rf"(?P<t>{_T})"
# (template_id, lang, kind, regex). Order matters only for overlapping patterns (none are ambiguous).
TEMPLATES = [
    # ---------------- Finnish
    ("FI_SHOWN_OPEN_WAS_CLOSED", "fi", "CLOSED", rf"Netissä luki auki {SH}, mutta {HOL} oli suljettu\."),
    ("FI_CLOSED_EARLY", "fi", "CLOSED_EARLY", rf"{HOL} suljettiin jo klo {TT}, vaikka netissä luki {SH}\."),
    ("FI_ARRIVED_CLOSED", "fi", "ARRIVED_CLOSED", rf"Tultiin {HOL} klo {TT} ja ovet olivat jo kiinni\."),
    ("FI_CAME_CLOSED_GOOGLE_OPEN", "fi", "CLOSED",
     rf"Tultiin {HOL} paikan päälle, mutta ovi oli kiinni, vaikka Googlen mukaan piti olla auki\."),
    ("FI_DROVE_DARK", "fi", "CLOSED", rf"Ajettiin varta vasten {HOL}, ja paikka oli pimeänä\."),
    ("FI_MAP_OPEN_REALLY_CLOSED", "fi", "CLOSED", rf"Kartan mukaan auki, todellisuudessa kiinni {HOL}\."),
    ("FI_MAP_CLAIMED_OPEN", "fi", "CLOSED", rf"Kartta väitti että olette auki {HOL}\."),
    ("FI_PITY_CLOSED", "fi", "CLOSED_COMPLAINT", rf"Harmi, {HOL} ovet kiinni\."),
    ("FI_CLOSED_ALTHOUGH_ONLINE_OPEN", "fi", "CLOSED", rf"{HOL} kiinni vaikka kaikkialla netissä luki auki\."),
    ("FI_WRONG_HOURS", "fi", "WRONG_HOURS", rf"Aukioloajat olivat väärin {HOL}\."),
    ("FI_THOUGHT_CLOSED_WAS_OPEN", "fi", "OPEN_LISTED_CLOSED",
     rf"Luulin että {HOL} on kiinni, koska Google niin näytti, mutta ohi kävellessä huomasin että olittekin auki!"),
    ("FI_ONLINE_CLOSED_WAS_OPEN", "fi", "OPEN_LISTED_CLOSED", rf"Netissä luki suljettu {HOL}, mutta olittekin auki\."),
    ("FI_THANKS_OPEN", "fi", "OPEN_THANKS", rf"Kiitos että olitte auki {HOL}, pelastitte päivän\."),
    ("FI_NICE_OPEN", "fi", "OPEN_THANKS", rf"Kiva että olitte auki {HOL}!"),
    ("FI_RARE_SERVICE_OPEN", "fi", "OPEN_THANKS", rf"Harvinaisen hyvä palvelus olla auki {HOL}\."),
    # ---------------- Swedish
    ("SV_SHOWN_OPEN_WAS_CLOSED", "sv", "CLOSED", rf"Det stod öppet {SH} på nätet men det var stängt {HOL}\."),
    ("SV_CLOSED_EARLY", "sv", "CLOSED_EARLY", rf"{HOL} stängde de redan {TT} fast det stod {SH} på nätet\."),
    ("SV_CAME_CLOSED_GOOGLE_OPEN", "sv", "CLOSED", rf"Vi kom hit {HOL} men det var stängt, trots att Google sa öppet\."),
    ("SV_DROVE_DARK", "sv", "CLOSED", rf"Åkte hit extra {HOL}, men allt var släckt\."),
    ("SV_PITY_CLOSED", "sv", "CLOSED_COMPLAINT", rf"Synd, stängt {HOL}\."),
    ("SV_WRONG_HOURS", "sv", "WRONG_HOURS", rf"Fel öppettider {HOL}\."),
    ("SV_THOUGHT_CLOSED_WAS_OPEN", "sv", "OPEN_LISTED_CLOSED",
     rf"Trodde det var stängt {HOL} eftersom Google sa så, men ni hade ju öppet!"),
    ("SV_NICE_OPEN", "sv", "OPEN_THANKS", rf"Skönt att ni hade öppet {HOL}!"),
    ("SV_THANKS_OPEN", "sv", "OPEN_THANKS", rf"Tack för att ni hade öppet {HOL}\."),
    # ---------------- Norwegian
    ("NB_SHOWN_OPEN_WAS_CLOSED", "nb", "CLOSED", rf"Det sto åpent {SH} på nett, men det var stengt {HOL}\."),
    ("NB_CLOSED_EARLY", "nb", "CLOSED_EARLY", rf"{HOL} stengte de allerede {TT} selv om det sto {SH} på nett\."),
    ("NB_CAME_CLOSED_GOOGLE_OPEN", "nb", "CLOSED",
     rf"Vi kom hit {HOL}, men det var stengt selv om Google sa at det var åpent\."),
    ("NB_DROVE_DARK", "nb", "CLOSED", rf"Kjørte hit {HOL}, men alt var mørkt\."),
    ("NB_PITY_CLOSED", "nb", "CLOSED_COMPLAINT", rf"Synd, stengt {HOL}\."),
    ("NB_WRONG_HOURS", "nb", "WRONG_HOURS", rf"Feil åpningstider {HOL}\."),
    ("NB_THOUGHT_CLOSED_WAS_OPEN", "nb", "OPEN_LISTED_CLOSED",
     rf"Trodde det var stengt {HOL} fordi Google sa det, men dere hadde jo åpent!"),
    ("NB_NICE_OPEN", "nb", "OPEN_THANKS", rf"Fint at dere hadde åpent {HOL}!"),
    ("NB_THANKS_OPEN", "nb", "OPEN_THANKS", rf"Takk for at dere holdt åpent {HOL}\."),
    # ---------------- English
    ("EN_SHOWN_OPEN_WAS_CLOSED", "en", "CLOSED", rf"Online it said open {SH}, but it was closed {HOL}\."),
    ("EN_CLOSED_EARLY", "en", "CLOSED_EARLY", rf"Closed already at {TT} {HOL} although online it said {SH}\."),
    ("EN_CAME_CLOSED_GOOGLE_OPEN", "en", "CLOSED", rf"Came here {HOL} but it was closed, even though Google said open\."),
    ("EN_MAP_SAID_OPEN", "en", "CLOSED", rf"Map said open {HOL}\."),
    ("EN_GOOGLE_CLOSED_WAS_OPEN", "en", "OPEN_LISTED_CLOSED", rf"Google said closed {HOL} but you were actually open\."),
    ("EN_NICE_OPEN", "en", "OPEN_THANKS", rf"Great that you were open {HOL}!"),
    ("EN_THANKS_OPEN", "en", "OPEN_THANKS", rf"Thanks for being open {HOL}\."),
]
TEMPLATES_RE = [(tid, lang, kind, re.compile(r"^" + rx + r"$")) for tid, lang, kind, rx in TEMPLATES]

# sentences that attach listed hours to the preceding CLOSED sentence ("Googlessa aukioloajat 7–18.")
SHOWN_SUFFIX = [
    ("FI_SUFFIX_GOOGLE_HOURS", "fi", re.compile(rf"^Googlessa aukioloajat {SH}\.$")),
    ("SV_SUFFIX_GOOGLE_HOURS", "sv", re.compile(rf"^Google visade {SH}\.$")),
    ("NB_SUFFIX_GOOGLE_HOURS", "nb", re.compile(rf"^Google viste {SH}\.$")),
    ("EN_SUFFIX_GOOGLE_HOURS", "en", re.compile(rf"^Google (?:said|showed) {SH}\.$")),
]

# review-level flags
FLAG_SENTENCES = {
    "CLOSED_FOR_GOOD": [
        "Lopettanut toimintansa, valitettavasti.", "Paikka on lopettanut, ovessa lappu ja tilat tyhjinä.",
        "Tämä on suljettu pysyvästi, älkää tulko turhaan.", "Stället har stängt för gott, lokalen är tom.",
        "Permanent stängt tyvärr.", "Stedet har lagt ned, lokalet er tomt.", "Stengt for godt dessverre.",
        "This place has closed down permanently.", "Permanently closed, the premises are empty.",
    ],
    "REGULAR_CHANGED": [
        "Aukioloajat ovat muuttuneet, netissä on vanhat tiedot.", "Uusi omistaja ja uudet aukioloajat, Googlessa vanhat.",
        "Öppettiderna har ändrats, nätet visar gamla tider.", "Åpningstidene er endret, nettet viser gamle tider.",
        "Opening hours have changed, online info is outdated.",
    ],
    "SEASONAL_CLOSED": [
        "Talvella kiinni, vaikka Google näyttää auki.", "Kesäravintola on tauolla, netissä silti aukioloajat.",
        "Stängt på vintern fast Google säger öppet.", "Stengt om vinteren selv om Google sier åpent.",
    ],
    # filler that only appears in hours complaints (kept so coverage can be measured on them too)
    "GENERIC_HOURS": [
        "Päivittäkää aukioloajat!", "Netissä oli eri ajat.", "Aukiolotiedot kannattaisi päivittää pyhien ajaksi.",
        "Tarkistakaa tiedot.", "Seisottiin suljetun oven takana.", "Turha reissu.", "Harmillista.", "Ei ollut.",
        "Melkein jäi tulematta.", "Uppdatera era öppettider!", "Uppdatera gärna helgtiderna.", "Onödig resa.",
        "Stod framför en stängd dörr.", "Oppdater åpningstidene!", "Oppdater gjerne åpningstidene for helligdager.",
        "Bortkastet tur.", "Sto foran en stengt dør.", "Please update your hours!", "Wasted trip.", "It wasn't.",
        "Almost didn't come.",
    ],
}
_FLAG_LOOKUP = {s: k for k, v in FLAG_SENTENCES.items() for s in v}
REGULAR_SPEC = [
    ("fi", re.compile(r"^Nykyään auki vain (?P<week>.+?), päivittäkää sivut\.$")),
    ("sv", re.compile(r"^Numera öppet bara (?P<week>.+?)\.$")),
    ("nb", re.compile(r"^Nå bare åpent (?P<week>.+?)\.$")),
    ("en", re.compile(r"^Now only open (?P<week>.+?)\.$")),
]
_DAYABBR = {
    # fi
    "ma": 0, "ti": 1, "ke": 2, "to": 3, "pe": 4, "la": 5, "su": 6,
    # sv
    "mån": 0, "tis": 1, "ons": 2, "tor": 3, "fre": 4, "lör": 5, "sön": 6,
    # nb
    "man": 0, "tir": 1, "fred": 4, "lør": 5, "søn": 6,
    # en
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_week(spec: str):
    """'ma–pe 9–17, la 10–14' -> {'monday': '09:00-17:00', ..., 'saturday': '10:00-14:00', 'sunday': 'closed'}.
    Days not mentioned are 'closed' (the template says 'now only open ...'). Returns None if unparsable."""
    week = {d: "closed" for d in DAYS}
    parts = re.split(r",\s*(?=[A-Za-zÅÄÖåäöøØæÆ]{2,4}(?:\s*[–-]\s*[A-Za-zÅÄÖåäöøØæÆ]{2,4})?\s+\d)", spec)
    ok = False
    for p in parts:
        m = re.match(r"^\s*([A-Za-zÅÄÖåäöøØæÆ]{2,4})(?:\s*[–-]\s*([A-Za-zÅÄÖåäöøØæÆ]{2,4}))?\s+(.+)$", p)
        if not m:
            return None
        a, b, hrs = m.group(1).lower(), (m.group(2) or m.group(1)).lower(), m.group(3)
        if a not in _DAYABBR or b not in _DAYABBR:
            return None
        h = norm_hours(hrs)
        if not h:
            return None
        for i in range(_DAYABBR[a], _DAYABBR[b] + 1):
            week[DAYS[i]] = h
        ok = True
    return week if ok else None


# --------------------------------------------------------------------------- sentence splitting
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-ZÅÄÖÆØ0-9\"])")


def split_sentences(text: str):
    return [s.strip() for s in _SPLIT.split(text or "") if s.strip()]


# --------------------------------------------------------------------------- main API
def _implied(kind, shown, t):
    """Implied true hours for the day, as far as the template states it."""
    if kind in ("CLOSED", "CLOSED_COMPLAINT"):
        return "closed"
    if kind == "CLOSED_EARLY" and shown and t:
        first = shown.split(",")[0]
        start = first.split("-")[0]
        tt = _hm(t)
        # keep intervals before T; cut the interval containing T
        out = []
        for iv in shown.split(","):
            a, b = iv.split("-")
            am, bm, tm = _to_min(a), _to_min(b), _to_min(tt)
            if bm <= am:
                bm += 1440
            if tm < am and tm + 1440 <= bm:
                tm += 1440
            if bm <= tm:
                out.append(iv)
            elif am < tm:
                out.append(f"{a}-{tt}")
        return ",".join(out) if out else f"{start}-{tt}"
    return None  # OPEN_* / WRONG_HOURS / ARRIVED_CLOSED: state open (or unknown) but hours not given


def parse_review(text: str) -> dict:
    """Parse one review text. Returns {'events': [...], 'flags': [...], 'regular_week': dict|None,
    'n_sentences', 'n_hours_sentences', 'unparsed_hours_sentences': [...]}."""
    sents = split_sentences(text)
    events, flags, week, unparsed = [], [], None, []
    n_hours = 0
    for s in sents:
        hit = False
        for tid, lang, kind, rx in TEMPLATES_RE:
            m = rx.match(s)
            if not m:
                continue
            gd = m.groupdict()
            dk, generic = map_holiday(gd["hol"])
            shown = norm_hours(gd.get("shown"))
            t = gd.get("t")
            ev = {"template": tid, "lang": lang, "kind": kind, "hol_text": gd["hol"], "day_key": dk,
                  "day_generic": generic, "shown_hours": shown,
                  "close_time": _hm(t) if (t and kind == "CLOSED_EARLY") else None,
                  "arrive_time": _hm(t) if (t and kind == "ARRIVED_CLOSED") else None}
            ev["implied_truth"] = _implied(kind, shown, t)
            ev["implied_state"] = ("closed" if kind in ("CLOSED", "CLOSED_COMPLAINT") else
                                   "open" if kind in ("CLOSED_EARLY", "OPEN_LISTED_CLOSED", "OPEN_THANKS") else None)
            ev["listing_state"] = ("open" if kind in ("CLOSED", "CLOSED_EARLY", "ARRIVED_CLOSED") else
                                   "closed" if kind == "OPEN_LISTED_CLOSED" else None)
            events.append(ev)
            hit = True
            break
        if not hit:
            for tid, lang, rx in SHOWN_SUFFIX:
                m = rx.match(s)
                if m:
                    hit = True
                    if events and events[-1]["shown_hours"] is None:
                        events[-1]["shown_hours"] = norm_hours(m.group("shown"))
                        events[-1]["template"] += "+HOURS"
                    break
        if not hit and s in _FLAG_LOOKUP:
            flags.append(_FLAG_LOOKUP[s])
            hit = True
        if not hit:
            for lang, rx in REGULAR_SPEC:
                m = rx.match(s)
                if m:
                    flags.append("REGULAR_CHANGED")
                    week = parse_week(m.group("week"))
                    hit = True
                    break
        if hit:
            n_hours += 1
        elif HOURS_KEYWORDS.search(s):
            unparsed.append(s)
    return {"events": events, "flags": flags, "regular_week": week, "n_sentences": len(sents),
            "n_hours_sentences": n_hours, "unparsed_hours_sentences": unparsed}


# broad keyword net used to define "hours-related" text for coverage measurement
HOURS_KEYWORDS = re.compile(
    r"auki|kiinni|suljet|aukiolo|netissä|googl|kartta|kartan|pimeänä|lopettanut|pysyvästi|"
    r"öppet|stängt|stängde|öppettid|släckt|för gott|"
    r"åpent|stengt|stengte|åpningstid|mørkt|lagt ned|for godt|"
    r"\bopen\b|closed|hours|online|\bmap\b",
    re.I)


def review_events(df, text_col="content"):
    """Vectorised helper: DataFrame of reviews -> long DataFrame with one row per parsed event (plus the
    original columns). Requires pandas."""
    import pandas as pd
    rows = []
    for idx, txt in zip(df.index, df[text_col].fillna("")):
        if not txt or not HOURS_KEYWORDS.search(txt):
            continue
        r = parse_review(txt)
        for ev in r["events"]:
            rows.append({"_idx": idx, **ev, "flags": "|".join(r["flags"])})
        if not r["events"] and r["flags"]:
            rows.append({"_idx": idx, "template": None, "kind": None, "flags": "|".join(r["flags"]),
                         "regular_week": r["regular_week"]})
    ev = pd.DataFrame(rows)
    if ev.empty:
        return ev
    return ev.join(df, on="_idx")


