"""Step 5b: tell the owner where the new hours show, and when.

Publisher list + typical update speed come from the mock's listings.json (the judges: "for the confirmation step,
it's best to use the listings.json from the mock dataset"). The real Yext Listings API has status but no speed.
"""
from __future__ import annotations

import json
from datetime import date
from functools import lru_cache

from . import hours as H
from .config import DATA

LANGS = ("fi", "sv", "nb", "en")

T = {
    "fi": {"short": "Valmista ✅ {name}: {days}. Näkyy: {first} + {n} muuta. Peru: vastaa PERU.",
           "done": "Valmista ✅ {name}:", "auto": "Päivitimme pysyvän lupanne mukaisesti ✅ {name}:",
           "shows": "Näkyy:", "more": "+{n} muuta palvelua", "undo": "Väärin? Vastaa PERU, niin palautamme entisen.",
           "closed": "kiinni", "regular": "normaalit aukioloajat", "open": "auki {h}",
           "already": "{name}: {d} oli jo ajan tasalla ({h}) – mitään ei tarvinnut muuttaa.",
           "conflict": "{name}: {d} on sillä välin merkitty muotoon {h}, joten emme muuttaneet sitä.",
           "failed": "{name}: {d} päivitys ei onnistunut. Olemme asian kimpussa, eikä sinun tarvitse tehdä mitään.",
           "undone": "Palautettu ↩️ {name}: {d} näyttää taas: {h}.",
           "units": ("min", "h", "vrk")},
    "sv": {"short": "Klart ✅ {name}: {days}. Syns på {first} + {n} till. Svara ÅNGRA för att ångra.",
           "done": "Klart ✅ {name}:", "auto": "Vi har uppdaterat enligt ditt stående godkännande ✅ {name}:",
           "shows": "Syns på:", "more": "+{n} tjänster till", "undo": "Fel? Svara ÅNGRA så återställer vi.",
           "closed": "stängt", "regular": "ordinarie öppettider", "open": "öppet {h}",
           "already": "{name}: {d} var redan uppdaterat ({h}) – inget behövde ändras.",
           "conflict": "{name}: {d} har under tiden satts till {h}, så vi ändrade inget.",
           "failed": "{name}: uppdateringen för {d} gick inte igenom. Vi löser det, du behöver inte göra något.",
           "undone": "Återställt ↩️ {name}: {d} visar igen: {h}.",
           "units": ("min", "tim", "dygn")},
    "nb": {"short": "Ferdig ✅ {name}: {days}. Vises på {first} + {n} til. Svar ANGRE for å angre.",
           "done": "Ferdig ✅ {name}:", "auto": "Vi har oppdatert etter din faste tillatelse ✅ {name}:",
           "shows": "Vises på:", "more": "+{n} tjenester til", "undo": "Feil? Svar ANGRE, så tilbakestiller vi.",
           "closed": "stengt", "regular": "vanlige åpningstider", "open": "åpent {h}",
           "already": "{name}: {d} var allerede oppdatert ({h}) – ingenting måtte endres.",
           "conflict": "{name}: {d} er i mellomtiden satt til {h}, så vi endret ingenting.",
           "failed": "{name}: oppdateringen for {d} gikk ikke gjennom. Vi fikser det, du trenger ikke gjøre noe.",
           "undone": "Tilbakestilt ↩️ {name}: {d} viser igjen: {h}.",
           "units": ("min", "t", "døgn")},
    "en": {"short": "Done ✅ {name}: {days}. Live on {first} + {n} more. Reply UNDO to undo.",
           "done": "Done ✅ {name}:", "auto": "Updated under your standing permission ✅ {name}:",
           "shows": "Shows on:", "more": "+{n} more services", "undo": "Wrong? Reply UNDO and we'll put it back.",
           "closed": "closed", "regular": "regular hours", "open": "open {h}",
           "already": "{name}: {d} was already up to date ({h}) – nothing needed changing.",
           "conflict": "{name}: {d} was set to {h} in the meantime, so we didn't change it.",
           "failed": "{name}: the update for {d} didn't go through. We're on it – nothing for you to do.",
           "undone": "Put back ↩️ {name}: {d} shows {h} again.",
           "units": ("min", "h", "days")},
}
# Replies that mean "undo the change you just made" (the confirmation message tells owners to send these).
UNDO_WORDS = {"peru", "peruuta", "ångra", "angra", "angre", "undo"}


def is_undo_reply(text: str) -> bool:
    """True when an owner's free-text reply is an undo request, e.g. 'PERU', 'undo!', 'Ångra'."""
    words = "".join(ch if ch.isalpha() else " " for ch in (text or "").lower()).split()
    return len(words) <= 3 and any(w in UNDO_WORDS for w in words)


MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@lru_cache(maxsize=1)
def _listings() -> dict[str, list[dict]]:
    with open(DATA / "listings.json", encoding="utf-8") as f:
        rows = json.load(f)["listings"]
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["entityId"], []).append(r)
    return out


def live_publishers(entity_id: str) -> list[dict]:
    """LIVE publishers for the business, fastest first: [{publisherId, publisherName, seconds}]."""
    rows = [r for r in _listings().get(entity_id, []) if r["status"] == "LIVE"]
    return [{"publisherId": r["publisherId"], "publisherName": r["publisherName"],
             "seconds": r["typicalUpdateSpeedSeconds"]} for r in sorted(rows, key=lambda r: r["typicalUpdateSpeedSeconds"])]


def eta(seconds: int, lang: str) -> str:
    m, h, d = T[lang]["units"]
    if seconds < 3600:
        return f"~{max(1, round(seconds / 60))} {m}"
    if seconds < 86400:
        return f"~{round(seconds / 3600)} {h}"
    n = round(seconds / 86400)
    return f"~{n} {'day' if lang == 'en' and n == 1 else d}"


def day_label(iso: str, lang: str) -> str:
    d = date.fromisoformat(iso)
    if lang == "en":
        return f"{d.day} {MONTHS_EN[d.month - 1]}"
    if lang == "sv":
        return f"{d.day}/{d.month}"
    return f"{d.day}.{d.month}."


def hours_label(s: str | None, lang: str) -> str:
    t = T[lang]
    if s is None or s == H.REGULAR:
        return t["regular"]
    if s == H.CLOSED:
        return t["closed"]
    return t["open"].format(h=H.pretty(s))


def lang_of(lang: str | None) -> str:
    return lang if lang in LANGS else "en"


def applied_message(name: str, applied: list[tuple[str, str]], entity_id: str, lang: str | None,
                    auto: bool = False, max_publishers: int = 4) -> str:
    """Owner confirmation after a successful write. `applied` = [(date, hours)]."""
    lang = lang_of(lang)
    t = T[lang]
    lines = [t["auto" if auto else "done"].format(name=name)]
    lines += [f"• {day_label(d, lang)} {hours_label(h, lang)}" for d, h in sorted(applied)]
    pubs = live_publishers(entity_id)
    if pubs:
        shown = ", ".join(f"{p['publisherName']} {eta(p['seconds'], lang)}" for p in pubs[:max_publishers])
        rest = len(pubs) - max_publishers
        lines.append(f"{t['shows']} {shown}" + (f", {t['more'].format(n=rest)}" if rest > 0 else "") + ".")
    lines.append(t["undo"])
    return "\n".join(lines)


def applied_message_short(name: str, applied: list[tuple[str, str]], entity_id: str, lang: str | None) -> str:
    """One-line confirmation for SMS / WhatsApp previews, e.g.
    'Done ✅ Kahvila Aurora: 24 Dec closed. Live on Google ~2 min + 8 more. Reply UNDO to undo.'"""
    lang = lang_of(lang)
    days = ", ".join(f"{day_label(d, lang)} {hours_label(h, lang)}" for d, h in sorted(applied))
    pubs = live_publishers(entity_id)
    if not pubs:
        return T[lang]["done"].format(name=name) + " " + days + ". " + T[lang]["undo"]
    first = f"{pubs[0]['publisherName'].split(' (')[0]} {eta(pubs[0]['seconds'], lang)}"
    return T[lang]["short"].format(name=name, days=days, first=first, n=len(pubs) - 1)


def status_message(kind: str, name: str, day: str, hrs: str | None, lang: str | None) -> str:
    """kind: already | conflict | failed | undone"""
    lang = lang_of(lang)
    return T[lang][kind].format(name=name, d=day_label(day, lang), h=hours_label(hrs, lang))
