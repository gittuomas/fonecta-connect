#!/usr/bin/env python3
"""Owner phone page: the "Contact → Decide → Apply" part of the loop, as a real tappable page.

    python3 app/app.py          # then open http://localhost:8000

Connect side (/)            pick a demo business and "send" the suggestion
Owner side  (/m/<id>/<date>) the message on the owner's phone: Approve / Edit / No thanks, no login
After a yes, the hours are written to Yext (the local mock by default) and the owner sees where they show.

Suggestions come from the team's C13 engine (engine/output); the challenge baseline is only a fallback.
Set YEXT_BASE_URL + YEXT_API_KEY to write to the real Yext sandbox instead of the local mock.
Standard library only.
"""
from __future__ import annotations

import html
import os
import re
import sys
import time
import urllib.parse
from datetime import date, datetime
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
MAT = os.path.join(os.path.dirname(HERE), "resources")
sys.path.insert(0, os.path.join(MAT, "mock_api"))
sys.path.insert(0, os.path.join(MAT, "examples"))
os.environ.setdefault("MOCK_QUIET", "1")

from baseline import Baseline  # noqa: E402
from quickstart import TXT, WD, day_key_for, eta  # noqa: E402
sys.path.insert(0, os.path.dirname(HERE))   # repository root, for the team's step5_apply package
from step5_apply import hours as H
from step5_apply.yext import YextError
from step5_apply.api import Step5  # noqa: E402
from step5_apply import special_days as SD  # noqa: E402
import search_value  # noqa: E402  (one short line from the business's own Google numbers)
import messaging
import hours_ai  # noqa: E402  (the AI model on Red Hat OpenShift AI that reads owners' replies)
import learning  # noqa: E402  (learns which reasons each owner wants: docs/new-customer-plan.md)
import proactive  # noqa: E402  (weather and season checks: who benefits comes from the challenge data)
from predictions import SelectedPredictions  # noqa: E402
from live_signals.owner import CheckFeed, suggestion as live_suggestion  # noqa: E402
import posts  # noqa: E402  (post suggestions on WhatsApp: holiday hours, events, Fonecta's monthly post)
import social_hours  # noqa: E402  (special hours from the owner's own Instagram posts)

MESSAGING = None
PROPOSALS = {}
PENDING_HOURS = {}
CHAT_TIMES = {}

READER = {}   # (channel, eid, pid) -> which reader understood the owner's last reply (shown on the panel)

PORT = int(os.environ.get("PORT", "8000"))

# Owner-facing words that the challenge's message templates don't cover.
UI = {
    "fi": {"now": "Nyt Googlessa", "sug": "Ehdotus", "approve": "Hyväksy", "edit": "Muokkaa", "no": "Ei kiitos",
           "closed_l": "Kiinni koko päivän", "open_l": "Auki", "from": "Alkaen", "to": "Asti", "save": "Tallenna",
           "back": "Takaisin", "done": "Valmis! Uudet aukioloajat päivitetään:", "declined": "Selvä, emme muuta mitään.",
           "declined2": "Google näyttää edelleen tavalliset aukioloajat tälle päivälle.",
           "always": "Hoida jatkossa juhlapyhät puolestani (ilmoitamme aina muutoksista)",
           "always_on": "Jatkossa hoidamme juhlapyhät puolestasi ja ilmoitamme jokaisesta muutoksesta.",
           "bad_time": "Sulkemisajan pitää olla avaamisajan jälkeen.", "from_c": "Fonecta Connect",
           "why": "Miksi ehdotamme tätä?", "safe": "Emme koskaan pyydä tunnuksia tai salasanaa."},
    "sv": {"now": "Nu på Google", "sug": "Förslag", "approve": "Godkänn", "edit": "Ändra", "no": "Nej tack",
           "closed_l": "Stängt hela dagen", "open_l": "Öppet", "from": "Från", "to": "Till", "save": "Spara",
           "back": "Tillbaka", "done": "Klart! De nya öppettiderna uppdateras på:", "declined": "Okej, vi ändrar ingenting.",
           "declined2": "Google visar fortfarande de vanliga öppettiderna för dagen.",
           "always": "Sköt helgdagarna åt mig i fortsättningen (vi meddelar alltid ändringar)",
           "always_on": "I fortsättningen sköter vi helgdagarna åt dig och meddelar varje ändring.",
           "bad_time": "Stängningstiden måste vara efter öppningstiden.", "from_c": "Fonecta Connect",
           "why": "Varför föreslår vi detta?", "safe": "Vi ber aldrig om inloggning eller lösenord."},
    "nb": {"now": "Nå på Google", "sug": "Forslag", "approve": "Godkjenn", "edit": "Endre", "no": "Nei takk",
           "closed_l": "Stengt hele dagen", "open_l": "Åpent", "from": "Fra", "to": "Til", "save": "Lagre",
           "back": "Tilbake", "done": "Ferdig! De nye åpningstidene oppdateres på:", "declined": "Greit, vi endrer ingenting.",
           "declined2": "Google viser fortsatt de vanlige åpningstidene for dagen.",
           "always": "Ta hånd om helligdager for meg fremover (vi varsler alltid om endringer)",
           "always_on": "Fremover tar vi hånd om helligdager for deg og varsler om hver endring.",
           "bad_time": "Stengetiden må være etter åpningstiden.", "from_c": "Fonecta Connect",
           "why": "Hvorfor foreslår vi dette?", "safe": "Vi ber aldri om innlogging eller passord."},
    "en": {"now": "Google now shows", "sug": "Suggestion", "approve": "Approve", "edit": "Edit", "no": "No thanks",
           "closed_l": "Closed all day", "open_l": "Open", "from": "From", "to": "Until", "save": "Save",
           "back": "Back", "done": "Done! Your new hours are updating on:", "declined": "OK, we won't change anything.",
           "declined2": "Google keeps showing your regular hours for that day.",
           "always": "Handle holidays for me from now on (we always tell you what changed)",
           "always_on": "From now on we'll handle holidays for you and tell you about every change.",
           "bad_time": "Closing time has to be after opening time.", "from_c": "Fonecta Connect",
           "why": "Why are we suggesting this?", "safe": "We never ask for logins or passwords."},
}

def nice(hours):
    """'07:30-13:00,15:00-18:00' -> '7:30–13, 15–18' (English time style)."""
    def t(x):
        hh, mm = x.split(":")
        return str(int(hh)) + ("" if mm == "00" else ":" + mm)
    return ", ".join("–".join(t(x) for x in iv.split("-")) for iv in hours.split(","))


def state(hours, lang):
    if hours == "regular":
        return "open your regular hours"
    return TXT[lang]["closed"] if hours == "closed" else TXT[lang]["open"].format(h=nice(hours))


BL = Baseline(2026)


def load_events():
    import json
    with open(os.path.join(MAT, "data", "local_events.json"), encoding="utf-8") as f:
        return {e["@id"]: e for e in json.load(f)["data"]}


def load_target_rows():
    import csv
    with open(os.path.join(MAT, "evaluation", "targets_2026.csv"), encoding="utf-8") as f:
        return {(r["entity_id"], r["date"]): r for r in csv.DictReader(f)}


def load_engine():
    """The team's suggestions: engine (C13). Demo businesses come from demo_suggestions.json, which has
    the evidence behind each suggestion; any other business from predictions_2026.csv (a missing row = NO_ACTION).
    Returns None when neither file exists, and the page falls back to the challenge baseline."""
    import csv
    import json
    out_dir = os.environ.get("ENGINE_OUTPUT", os.path.join(os.path.dirname(HERE), "engine", "output"))
    demo_p, csv_p = os.path.join(out_dir, "demo_suggestions.json"), os.path.join(out_dir, "predictions_2026.csv")
    if not os.path.exists(csv_p):
        csv_p = os.path.join(os.path.dirname(HERE), "predictions_2026.csv")   # the submitted copy at the root
    rows, covered = {}, set()
    if os.path.exists(csv_p):
        with open(csv_p, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[(r["entity_id"], r["date"])] = {"action": r["action"], "hours": r["hours"] or "",
                                                     "evidence": [], "confidence": None}
                covered.add(r["entity_id"])
    if os.path.exists(demo_p):
        with open(demo_p, encoding="utf-8") as f:
            for r in json.load(f):
                rows[(r["entity_id"], r["date"])] = {"action": r["action"], "hours": r["proposed_hours"] or "",
                                                     "evidence": r["evidence"], "confidence": r["confidence"]}
                covered.add(r["entity_id"])
    return (rows, covered) if rows else None


ENGINE = load_engine()


def engine_row(eid, day):
    if (eid, day.isoformat()) in DEMO_TARGETS:
        row = PREDICTIONS.predict(eid, day)
        # Keep the team's reason wording; name last year only when the recorded hours match.
        if row['last_hours'] is None:
            row['evidence'] = [e for e in row.get('evidence', []) if e['source'] != 'LAST_YEAR_ENTRY']
        return row
    if not ENGINE:
        return None
    return ENGINE[0].get((eid, day.isoformat()), {"action": "NO_ACTION", "hours": "", "evidence": [], "confidence": None})


# Engine evidence → one owner-facing reason. Team decision: only the owner's own history (and their own
# customers' reviews) is named; what other businesses chose (peers, shopping centre) stays behind the scenes.
OWN_REASONS = [
    ("LAST_YEAR_ENTRY", None),   # "Last year you were open 10–14, …" (the challenge's own wording)
    ("OLDER_ENTRY", "You've used these hours for this day before."),
    ("OWN_HABIT_REAPPLIED", "This matches how you usually open on holidays."),
    ("STRUCTURAL_SCHOOL", "This matches your latest school-holiday hours."),
    ("OWN_HOURS_HISTORY", "These are the special hours you've used most often."),
    ("STRUCTURAL_TERRACE", "Your terrace is listed as seasonal, and no winter reopening is set."),
    ("REVIEW_WEEKLY_HOURS", "A recent customer review mentions these hours."),
]


def engine_reason(eng, sug, lang):
    """(reason, short, last_hours) for one engine suggestion."""
    sources = {e["source"] for e in eng["evidence"]}
    for src, text in OWN_REASONS:
        if src in sources:
            if src == "LAST_YEAR_ENTRY":
                last = state(sug, lang)
                return TXT[lang]["LAST_YEAR"].format(last=last), f"Last year you were {last}.", sug
            return text + " No special hours are set for this day yet.", text, None
    return "No special hours are set for this day yet.", "", None


EVENTS = load_events()
TARGET_ROWS = load_target_rows()
DECISIONS = {}        # (eid, date) -> what the owner did, shown on the Connect side
DELIVERY = {}         # (eid, period) -> verified Apply/UNDO result, for the operator only
CHECK_SESSIONS = {}   # freeze a weather/season question once answered, so feedback cannot remove its reply/UNDO route


# Step 5 (Apply) is the team's step5_apply package: it writes to Yext (its own mock, plus the real
# sandbox when .env says so), reads the change back, keeps an audit log, writes the confirmation
# message and handles UNDO. Its saved state is in step5_apply/.runtime/ (delete it for a fresh demo).
DEMO_MODE = __name__ == '__main__' and '--demo' in sys.argv
if DEMO_MODE:
    import tempfile
    from step5_apply.mock_runtime import start_mock
    DEMO_SESSION = tempfile.TemporaryDirectory(prefix='fonecta-demo-')
    demo_mock = start_mock(DEMO_SESSION.name)
    S5 = Step5.from_env(mode='demo', use_sandbox=False, mock_url=demo_mock.url,
                        runtime_dir=DEMO_SESSION.name)
    S5.mock = demo_mock
else:
    S5 = Step5.from_env()
LIVE = CheckFeed(os.environ.get('CONNECT_LIVE_CHECKS'), sandbox=bool(S5.applier.sandbox or S5.applier.primary.name == 'sandbox'),
                 state_path=S5.applier.audit.path.parent / 'live_checks_seen.json')
SEEN = {}   # (eid, pid) -> step 5 snapshot of the dates when the message was first shown ("seen_before")


def seen_for(eid, pid, items):
    if (eid, pid) not in SEEN:
        SEEN[(eid, pid)] = S5.snapshot(eid, [s["day"].isoformat() for s in items])
        if items[0].get('live_check'):
            LIVE.presented(eid, pid)
    return SEEN[(eid, pid)]


def apply_via_step5(eid, pid, channel, decisions, remember=False):
    """decisions: [(suggestion, "ACCEPTED" | "EDITED" | "REJECTED", hours or None)] -> step 5 result."""
    import uuid
    seen = seen_for(eid, pid, [d[0] for d in decisions])
    items = []
    for sug, decision, hours in decisions:
        k = sug["day"].isoformat()
        item = {"date": k, "decision": decision, "seen_before": seen.get(k)}
        if sug.get('check') or sug.get('live_check'):
            item['day_key'] = sug['key']
        if hours is not None:
            item["hours"] = hours
        items.append(item)
    live = any(s.get('live_check') for s, _, _ in decisions)
    res = S5.apply({"entity_id": eid, "suggestion_id": f"{channel.lower()}-{eid}-{pid}-{uuid.uuid4().hex[:8]}",
                     "channel": channel, "actor": "owner",
                     "remember_yearly": remember and not any(s.get('live_check') or s.get('check') for s, _, _ in decisions), "language": "en",
                     "items": items})
    if live:
        S5.applier.audit.append({'type': 'LIVE_CHECK_DECISION', 'entity_id': eid, 'check_id': pid,
            'mode': LIVE.mode, 'apply_audit_id': res.get('audit_id'),
            'event_ids': [s['live_check']['event_id'] for s, _, _ in decisions],
            'owner_decisions': [{'date': s['day'].isoformat(), 'decision': decision, 'hours': hrs}
                                for s, decision, hrs in decisions]})
    for item in res.get('items', []):
        if item['status'] in ('APPLIED', 'ALREADY_SET'):
            SEEN[(eid, pid)][item['date']] = item.get('hours')
    res = browser_result(res)
    DELIVERY[(eid, pid)] = res
    return res


def delivery_status(eid, pid):
    """Keep real Yext verification distinct from the mock and publisher estimates."""
    result = DELIVERY.get((eid, pid))
    if S5.applier.primary.name == 'sandbox':
        target = S5.applier.primary.mapping.get(eid)
        if not target:
            return 'No Yext sandbox mapping for this business'
        if result and any(r['status'] in ('APPLIED', 'UNDONE', 'ALREADY_SET') for r in result['items']):
            return f'Yext sandbox read-back verified: {target}. Public publication unverified.'
        return f'Real Yext sandbox: {target}. Public publication unverified.'
    target = S5.applier.mirror_map.get(eid) if S5.applier.sandbox else None
    if result:
        mirror = result.get('mirror')
        if mirror:
            if mirror.get('ok'):
                action = 'undo verified' if any(r['status'] == 'UNDONE' for r in result['items']) else 'write verified'
                return f"Yext sandbox {action}: {mirror['entity_id']}"
            detail = mirror.get('reason') or '; '.join(mirror.get('errors', {}).values())
            return f"Yext sandbox needs attention: {mirror['entity_id']}. {detail}"
        if all(r['status'] in ('DECLINED', 'NO_RESPONSE') for r in result['items']):
            return 'No listing update requested'
        if any(r['status'] in ('FAILED', 'INVALID', 'CONFLICT', 'BLOCKED') for r in result['items']):
            return 'Listing update needs attention; inspect the Apply audit'
    return f'Yext sandbox mapped: {target}' if target else 'Local mock only; no Yext sandbox mapping'


def live_weather_text(row):
    weather = LIVE.weather_for(row, BL.ents[row['entity_id']])
    if not weather:
        return 'Live weather unavailable or expired; no weather assumption used.'
    fields = [('tmean_c', 'mean', '°C'), ('precip_mm', 'precipitation', ' mm'),
              ('snow_depth_cm', 'snow depth', ' cm'), ('snowfall_cm', 'snowfall', ' cm')]
    values = ', '.join(f'{name} {weather[key]}{unit}' for key, name, unit in fields if weather.get(key) not in ('', None))
    return f"Open-Meteo forecast · {weather['city']} · {weather['date']}: {values}. Context only."


def _when(sec):
    if sec < 3600:
        return f"about {max(1, round(sec / 60))} minutes"
    if sec < 86400:
        return "an hour" if sec < 2 * 3600 else f"{round(sec / 3600)} hours"
    return "a day" if sec <= 86400 else ("a week" if sec >= 5 * 86400 else f"{round(sec / 86400)} days")


def short_where(eid):
    """Team decision: one short line instead of the list of every site and its timing, e.g.
    'Publisher timings are estimates: Google usually within minutes, and on 8 other sites soon after.'"""
    pubs = where_it_shows(eid)
    if not pubs:
        return ""
    others = len(pubs) - 1
    return ("Publisher timings are estimates: Google usually within minutes"
            + (f", and on {others} other site{'s' if others > 1 else ''} soon after." if others else "."))


WHERE_LINE = re.compile(r"^\s*(shows on|expected on)\b", re.I)                  # step 5's full list, any wording
WHERE_SHORT = re.compile(r"\s*(Live on|Expected on|Shows on) [^.]*?\+ ?\d+ more( services)?\.", re.I)


def without_where(msg, eid=None):
    """Team decision: no long list of where the hours show ("Shows on: Google ~2 min, Finder.fi ~10 min, ..."
    or "Expected on: ..."). Step 5's confirmation includes that list; here it becomes one short line
    (or nothing without eid). Step 5 itself is unchanged."""
    if not msg:
        return msg
    out = []
    for l in msg.splitlines():
        if WHERE_LINE.match(l):
            if eid:
                out.append(short_where(eid))
        else:
            out.append(WHERE_SHORT.sub(" " + short_where(eid) if eid else "", l))
    return "\n".join(out)


def where_it_shows(eid):
    return [(p["publisherName"], p["seconds"]) for p in S5.where_it_shows(eid)]


def problems_text(res):
    """Owner-friendly notes for dates step 5 didn't apply as asked."""
    notes = []
    for it in res.get("items", []):
        if it["status"] == "CONFLICT":
            notes.append(f"{it['date']}: you had already changed this yourself, so we kept your version.")
        elif it["status"] in ("FAILED", "INVALID", "BLOCKED"):
            notes.append(f"{it['date']}: couldn't be saved ({it['reason'] or it['status'].lower()}). A Fonecta person will check it.")
    return notes


@lru_cache(maxsize=None)
def opted_out(eid):
    try:
        return S5.policy(eid)["opted_out"]
    except Exception:
        return False


# Team decision: holidays whose English name isn't familiar (or is only a description) use the country's
# own name, e.g. Tapaninpäivä / Annandag jul / Andre juledag instead of "Boxing Day". Well-known ones stay
# in English: Christmas Eve, Christmas Day, New Year's Eve, New Year's Day, Easter, Midsummer, ...
LOCAL_NAME_KEYS = {"BOXING_DAY", "EPIPHANY", "EPIPHANY_EVE", "DEC23", "INTERDAY", "LUCIA", "MAUNDY_THURSDAY",
                   "ASCENSION", "PENTECOST", "PENTECOST_EVE", "WHIT_MONDAY", "MAY_DAY", "MAY_EVE"}


def holiday_name(country, day, lang="en"):
    """The name to show owners for a special day, or None if the day isn't in the calendar."""
    for r in BL.cal:
        if r["date"] == day.isoformat() and r["country"] == country:
            return r["name_local"] if lang != "en" or r["day_key"] in LOCAL_NAME_KEYS else r["name_en"]
    return None


@lru_cache(maxsize=None)   # the data doesn't change while the page runs
def suggestion(eid: str, day: date) -> dict:
    """Everything the owner page needs for one business and one day."""
    ent = BL.ents[eid]
    country = ent["meta"]["countryCode"]
    # Team decision: English everywhere for the demo. Fonecta adds other languages later
    # (switch back with: ent["meta"]["language"] if ent["meta"]["language"] in TXT else "en").
    lang = "en"
    row = TARGET_ROWS.get((eid, day.isoformat()))   # fast lookup instead of re-reading the targets file
    key = row["day_key"] if row else day_key_for(BL, eid, country, day)
    shown = BL.displayed(eid, day)
    eng = engine_row(eid, day)
    if eng:   # the team's C13 engine (engine); the challenge baseline only if its output is missing
        shown = eng.get('shown', shown)
        act, sug, basis, ev = eng["action"], eng["hours"], "ENGINE", None
    else:
        act, sug, basis, ev = BL.explain(eid, day, key) if key else ("NO_ACTION", "", None, None)
    t = TXT[lang]
    day_name = holiday_name(country, day, lang) or key
    event = None
    if key == "EVENT":
        row = TARGET_ROWS.get((eid, day.isoformat()), {})
        event = EVENTS.get(row.get("event_id"))
        day_name = event["name"]["en"] if event else "Local event"
    elif key == "SCHOOL_BREAK":
        day_name = "School holiday"
    if lang == "en":
        dm = f"{day.day} {day.strftime('%b')}"
    else:
        dm = f"{day.day}/{day.month}" if lang == "sv" else f"{day.day}.{day.month}."
    reason, short, last_hours = "", "", None
    if act == "SUGGEST" and eng:
        reason, short, last_hours = engine_reason(eng, sug, lang)
    elif act == "SUGGEST":
        last = ""
        if basis == "LAST_YEAR":
            last = "open your regular hours" if ev[1] == "REGULAR" else state(ev[1], lang)
        # Team decision: never show owners what other businesses (shopping centre, peers) have chosen.
        # Only the owner's own history is named as a reason.
        if basis == "LAST_YEAR":
            last_hours = sug if ev[1] == "REGULAR" else ev[1]
            reason = t["LAST_YEAR"].format(last=last)
            short = f"Last year you were {last}."
        else:
            reason = "No special hours are set for this day yet."
            short = ""
    return {"eid": eid, "name": ent["name"], "city": ent["address"]["city"], "lang": lang, "day": day,
            "day_name": day_name, "dm": dm, "weekday": WD[lang][day.weekday()], "shown": shown,
            "action": act, "hours": sug, "basis": basis, "reason": reason, "short": short, "key": key,
            "last_hours": last_hours, "event": event,
            "contact_eligible": eng.get('contact_eligible', act == 'SUGGEST') if eng else act == 'SUGGEST',
            "source": "C13 engine" if eng else "challenge baseline",
            "evidence": eng["evidence"] if eng else [], "confidence": eng["confidence"] if eng else None,
            "intro": t["day"].format(day=day_name, wd=WD[lang][day.weekday()], dm=dm).rstrip(".") + ".",
            "shows": t["shows"].format(shown=state(shown, lang))}


def load_targets():
    import csv
    rows = {}
    with open(os.path.join(MAT, "evaluation", "targets_2026.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.setdefault(r["entity_id"], set()).add(date.fromisoformat(r["date"]))
    return {k: sorted(v) for k, v in rows.items()}


TARGETS = load_targets()


def period_of(s):
    """Contact plan step 6: one message per holiday period. Returns (period id, title)."""
    d = s["day"]
    # School-holiday days around Christmas (21-22 Dec, early Jan) join the holiday messages.
    if d.month == 12 and 21 <= d.day <= 30:
        return "christmas", "Christmas"
    if (d.month == 12 and d.day == 31) or (d.month == 1 and d.day <= 8):
        return "new-year", "New Year"
    if s["event"]:
        return "event-" + s["event"]["@id"].split(":")[-1], s["day_name"]
    return d.isoformat(), s["day_name"]


def intro(pid, title, items):
    """First sentence of every message: holiday period, local event, or a single special day."""
    s = items[0]
    if s.get('check'):
        return s['check']['intro']
    if s.get('live_check'):
        return f"On {s['weekday']} {s['dm']}, {s['reason']}"
    when = f"{s['weekday']} {s['dm']}"
    if pid in ("christmas", "new-year"):
        return f"{title} is coming up."
    if s["event"]:
        e = s["event"]
        venue = e.get("location", {}).get("name", {}).get("en", "")
        crowd = e.get("x_expected_attendance")
        return (f"There's an event nearby on {when}: {title}{' at ' + venue if venue else ''}"
                + (f", with about {crowd:,} visitors expected." if crowd else "."))
    if s["key"] == "SCHOOL_BREAK":
        return f"It's a school holiday on {when}."
    return f"{title} is on {when}."


def bundles_for(eid, source=None):
    """All messages for one business: holiday periods from the engine, plus the proactive weather and season
    checks (recomputed every time, because how proactive they are depends on what the owner has done)."""
    source = source or S5
    if source.applier.primary.name == 'sandbox' and eid not in source.applier.primary.mapping:
        return []
    out = {pid: (pid, title, list(items)) for pid, title, items in _calendar_bundles(eid)}
    candidates = [s for _, _, items in out.values() for s in items if not s.get('live_check') and not s.get('check')]
    current = source.snapshot(eid, [s['day'].isoformat() for s in candidates]) if candidates else {}
    for pid, title, items in list(out.values()):
        kept = []
        for item in items:
            if item.get('live_check') or item.get('check'):
                kept.append(item)
                continue
            saved = current[item['day'].isoformat()]
            if saved == item['hours']:
                continue
            kept.append(dict(item, seen_before=saved, has_exception=saved is not None,
                             shown=saved if saved not in (None, 'regular') else item['shown']))
        if kept:
            out[pid] = (pid, title, kept)
        else:
            del out[pid]
    if opted_out(eid) or LEARN.mode(eid) == "stopped" or eid in REVIEW_BUSINESSES or BL.ents[eid].get('closed'):
        return sorted(out.values(), key=lambda b: b[2][0]["day"])
    ent, scores = BL.ents[eid], LEARN.owner(eid)["scores"]
    sc = proactive.season_check(eid, ent, bool(ent["hours"].get("reopenDate")), scores.get("season", 0.0),
                                LEARN.reason_on(eid, "season"))
    if sc:
        # One question replaces the separate holiday messages for the break: "Are you closed for the winter?"
        days = [d for d in TARGETS.get(eid, []) if proactive.today() < d < sc["reopen"]]
        items = [check_item(eid, d, suggestion(eid, d)["shown"], "closed", "SEASON", sc) for d in days]
        items = [x for x in items if x["shown"] != "closed"]
        if items:
            gone = {x["day"] for x in items}
            for pid in list(out):
                out[pid] = (pid, out[pid][1], [x for x in out[pid][2] if x["day"] not in gone])
                if not out[pid][2]:
                    del out[pid]
            out["season"] = ("season", sc["title"], items)
    live_weather = LIVE.mode != 'disabled' and ent.get('c_segment') in proactive.SNOW_DEPENDENT
    wc = proactive.weather_check(eid, ent, lambda d: BL.displayed(eid, d), scores.get("weather", 0.0),
                                 LEARN.reason_on(eid, "weather"),
                                 forecast=LIVE.weather_for_city(ent['address']['city']) if live_weather else None,
                                 decision_date=LIVE.now().astimezone(ZoneInfo({'FI': 'Europe/Helsinki', 'SE': 'Europe/Stockholm',
                                                                            'NO': 'Europe/Oslo'}[ent['meta']['countryCode']])).date() if live_weather else None)
    if wc and (engine_row(eid, wc['day']) or {}).get('action') not in ('SUGGEST', 'FLAG'):
        out[wc["pid"]] = (wc["pid"], wc["title"], [check_item(eid, wc["day"], wc["shown"], wc["hours"], "WEATHER", wc)])
    # One message per day: days the owner announced on their own Instagram are handled by the Instagram
    # message (or need nothing, when Google already matches), so they leave the holiday messages.
    # A holiday chat the owner has already answered stays as it is.
    ig = instagram_days(eid)
    for pid in list(out):
        IG_REPLACED.pop((eid, pid), None)
        if not ig or any((c, eid, pid) in WA for c in ("w", "s", "e", "p")):
            continue
        keep = [x for x in out[pid][2] if x["day"] not in ig]
        if len(keep) < len(out[pid][2]):
            IG_REPLACED[(eid, pid)] = (out[pid][1], len(out[pid][2]) - len(keep), len(keep))
            if keep:
                out[pid] = (pid, out[pid][1], keep)
            else:
                del out[pid]
    return sorted(out.values(), key=lambda b: b[2][0]["day"])


bundles_for.cache_clear = lambda: _calendar_bundles.cache_clear()   # tests reset caches this way


def check_item(eid, day, shown, hours, key, chk):
    """A weather or season check in the same shape as an engine suggestion, so every channel page works."""
    base = suggestion(eid, day)
    return dict(base, shown=shown, hours=hours, key=key, action="CHECK_HOURS", contact_eligible=True,
                basis="PROACTIVE", reason=chk["why"], short="", last_hours=None, event=None,
                source=f"Proactive {chk['kind']} check", check=chk)


def is_check(eid, pid):
    return pid == "season" or pid.startswith("weather-")


@lru_cache(maxsize=None)
def _calendar_bundles(eid):
    """Engine suggestions grouped into holiday periods: [(pid, title, [suggestions])]."""
    out = {}
    for d in TARGETS.get(eid, []):
        s = suggestion(eid, d)
        if s["action"] == "SUGGEST" and s["contact_eligible"]:
            pid, title = period_of(s)
            out.setdefault(pid, (pid, title, []))[2].append(s)
    for (business, pid), row in LIVE.rows.items():
        if business == eid:
            s = live_suggestion(row, BL.ents[eid])
            out[pid] = (pid, s['day_name'], [s])
    return sorted(out.values(), key=lambda b: b[2][0]["day"])


def season_value(eid):
    """Reminder line for the season check: the business's own Google numbers since the season ended."""
    try:
        import pyarrow.parquet as pq
        t = pq.read_table(os.path.join(MAT, "data", "analytics_daily.parquet"),
                          columns=["date", "GOOGLE_SEARCH_VIEWS", "GOOGLE_MAP_VIEWS", "GOOGLE_PHONE_CALLS"],
                          filters=[("entity_id", "=", eid)]).to_pydict()
    except Exception:
        return ""
    last = list(zip(t["GOOGLE_SEARCH_VIEWS"], t["GOOGLE_MAP_VIEWS"], t["GOOGLE_PHONE_CALLS"]))[-28:]
    views, calls = sum((a or 0) + (b or 0) for a, b, _ in last), sum(c or 0 for _, _, c in last)
    if views < 10:
        return ""
    line = f"In the last 4 weeks, about {views // 10 * 10} people looked you up on Google"
    return line + (f" and {calls} called you" if calls else "") + ", while Google showed you open."


@lru_cache(maxsize=None)
def _value(eid, pid):
    if pid == "season":
        return season_value(eid) or "Many people check opening hours on Google. If they're wrong, customers may find a closed door."
    b = next((x for x in bundles_for(eid) if x[0] == pid), None)
    try:
        return search_value.value_line(MAT, BL, eid, pid, b[1], b[2]) if b else ""
    except ImportError:          # pandas/pyarrow not installed: the general reason, no numbers
        return ("Many people check opening hours on Google before the holidays. "
                "If they're wrong, customers may find a closed door.")


def value(eid, pid, title, items):
    """One short line from the business's own Google numbers (see search_value.py)."""
    return _value(eid, pid)


def bundle(eid, pid):
    if (eid, pid) in PROPOSALS:
        return PROPOSALS[(eid, pid)]
    if (eid, pid) in CHECK_SESSIONS and any((ch, eid, pid) in WA for ch in ('w', 's', 'e', 'p')):
        return CHECK_SESSIONS[(eid, pid)]
    if (eid, pid) in LIVE.rows and not any((ch, eid, pid) in WA for ch in ('w', 's', 'e', 'p')):
        if live_block(eid, pid):
            return None
    result = next((b for b in bundles_for(eid) if b[0] == pid), None)
    if result and result[2][0].get('check'):
        CHECK_SESSIONS[(eid, pid)] = result
    elif result and not result[2][0].get('live_check'):
        PROPOSALS[(eid, pid)] = result
        SEEN[(eid, pid)] = {x['day'].isoformat(): x['seen_before'] for x in result[2]}
    return result


def live_block(eid, pid):
    row = LIVE.rows[(eid, pid)]
    if opted_out(eid) or LEARN.mode(eid) != 'normal' or not LEARN.reason_on(eid, 'event'):
        return 'Owner contact preferences'
    if any(r.get('message') == pid for r in LEARN.owner(eid)['history']):
        return 'Already answered or completed'
    reason = LIVE.cooldown(eid, pid) or LIVE.unavailable(row, BL.ents[eid])
    if reason:
        return reason
    # A dated exception entered by the owner takes priority over another check.
    if (eid, pid) not in SEEN and S5.snapshot(eid, [row['date']]).get(row['date']) is not None:
        return 'Owner already set hours for this date'
    return ''


DEMO = sorted(e for e in TARGETS if "-D00" in e)
DEMO_TARGETS = {k: r for k, r in TARGET_ROWS.items() if k[0] in DEMO}
PREDICTIONS = SelectedPredictions(
    os.environ.get('CONNECT_SUGGESTIONS_PATH') or os.path.join(
        os.environ.get('ENGINE_OUTPUT') or os.path.join(os.path.dirname(HERE), 'engine', 'output'),
        'demo_suggestions.json'),
    DEMO_TARGETS, BL.ents, BL.hol, BL.reg, BL.cal,
)
if S5.applier.primary.name == 'sandbox':
    DEMO = [eid for eid in DEMO if eid in S5.applier.primary.mapping]

REVIEW_BUSINESSES = {eid for (eid, _), row in (ENGINE[0] if ENGINE else {}).items() if row['action'] == 'FLAG'}

# ---------------------------------------------------------------- HTML

CSS = """
:root{--navy:#14198a;--ink:#1b1b22;--muted:#5d5f6b;--line:#e3e4ea;--bg:#f4f4f7;--card:#fff;--ok:#1d7a4a;--okbg:#e6f4ec}
@media (prefers-color-scheme:dark){:root{--navy:#8f96ff;--ink:#ececf1;--muted:#a3a5b1;--line:#34353f;--bg:#15161b;--card:#1f2027;--ok:#6fd39c;--okbg:#17321f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:420px;margin:0 auto;padding:16px}
.wide{max-width:900px}
.brand{font-weight:700;color:var(--navy);font-size:15px;display:flex;align-items:center;gap:8px;margin:4px 0 14px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--navy)}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px}
h1{font-size:20px;margin:0 0 8px}h2{font-size:17px;margin:0 0 8px}
p{margin:0 0 12px}.muted{color:var(--muted);font-size:14px}
.cmp{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:14px 0}
.cmp div{border-radius:12px;padding:10px 12px;border:1px solid var(--line)}
.cmp .new{border:2px solid var(--navy)}
.cmp small{display:block;color:var(--muted);font-size:12px}.cmp b{font-size:18px}
.old b{text-decoration:line-through;text-decoration-color:var(--muted);color:var(--muted)}
.btn{display:block;width:100%;padding:14px;border-radius:12px;font-size:17px;font-weight:600;text-align:center;
 border:1.5px solid var(--navy);background:transparent;color:var(--navy);text-decoration:none;cursor:pointer;margin-top:10px;font-family:inherit}
.btn.primary{background:var(--navy);color:#fff}
@media (prefers-color-scheme:dark){.btn.primary{color:#0e0f2e}}
.btn.plain{border-color:var(--line);color:var(--muted)}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
label{display:block;font-size:14px;color:var(--muted);margin:10px 0 4px}
input[type=time]{width:100%;font-size:18px;padding:10px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--ink)}
.opt{display:flex;gap:10px;align-items:flex-start;padding:12px;border:1px solid var(--line);border-radius:12px;margin-top:10px;font-size:15px}
.opt input{width:20px;height:20px;margin-top:2px;accent-color:var(--navy)}
.err{color:#c62828;font-size:14px;margin-top:8px}
.ok{background:var(--okbg);color:var(--ok);border-radius:12px;padding:12px;font-weight:600;margin-bottom:12px}
ul.pubs{list-style:none;padding:0;margin:0}ul.pubs li{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--line);font-size:15px}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
.tag{font-size:12px;padding:2px 8px;border-radius:99px;background:var(--bg);border:1px solid var(--line);white-space:nowrap}
a{color:var(--navy)}
.day{border:1px solid var(--line);border-radius:12px;padding:12px;margin-top:10px}
.day .when{font-weight:600;font-size:15px}
.day .hrs{margin:4px 0 2px;font-size:15px}.day .hrs s{color:var(--muted)}.day .hrs b{color:var(--navy)}
.day .why{font-size:13px;color:var(--muted)}
.day.bad{border-color:#c62828}
details{margin-top:8px}summary{color:var(--navy);font-size:14px;cursor:pointer}
details .ch{display:flex;gap:8px;align-items:center;font-size:15px;margin:8px 0 0}
details .ch input[type=radio]{width:18px;height:18px;accent-color:var(--navy)}
details .times{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:6px 0 0 26px}
details input[type=time]{font-size:16px;padding:8px}
ul.res{list-style:none;padding:0;margin:0 0 14px}ul.res li{padding:6px 0;border-bottom:1px solid var(--line);font-size:15px}
"""


def page(title, body, wide=False):
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title><style>{CSS}</style></head>"
            f"<body><div class='wrap{' wide' if wide else ''}'>{body}</div></body></html>")


def h(s):
    return html.escape(str(s))


# ---------------------------------------------------------------- when to contact (team decision)
# First message about 3 weeks before the first day in it, at the start of the owner's best_contact_window
# but never after 17:00 (pilot: evening messages got the fewest replies). If there's no reply:
# a reminder on WhatsApp for everyone; only if urgent, an AI voice call 2 days later and one more the next day; then stop.
SEND_WEEKS_BEFORE = 3
LATEST_SEND_HOUR = 17
# Days to wait before the reminder, from how fast owners replied on each channel in the 2025 pilot
# (90% of replies came within: WhatsApp 4 h, SMS 5 h, app push 25 h, email 2 days).
REMINDER_AFTER_DAYS = {"WHATSAPP": 1, "SMS": 1, "APP_PUSH": 1, "EMAIL": 2}
CALL_AFTER_REMINDER_DAYS = 2
# Urgent calls happen at least 7 days before the day: Google updates in minutes, but Apple Maps, Tripadvisor
# and AI assistants take up to a week (step 5's "where it shows" list). Later than that is too late for most of them.
CALL_LATEST_DAYS_BEFORE = 7


def send_hour(window):
    """'16-20' -> 16. Start of the owner's window, but no later than 17:00. No window -> 10:00."""
    try:
        start = int(str(window).split("-")[0])
    except ValueError:
        return 10
    return min(start, LATEST_SEND_HOUR)


def load_spikes():
    """Search views on the same day last year vs a normal day (made by make_search_spikes.py)."""
    import json
    try:
        with open(os.path.join(HERE, "search_spikes.json"), encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return {}


SPIKES = load_spikes()
SPIKE_URGENT = 2.0        # "more searches than usual": at least twice the business's normal day
REVIEW_MAX_STARS = 3      # a complaint review has 3 stars or fewer
REVIEW_NEAR_DAYS = 10     # ...and was written within 10 days of the same date in an earlier year

# Reviews that say the place was closed or had wrong hours (fi / sv / nb / en). "Closed for good" reviews are
# left out: those businesses go to a Fonecta person, not an AI call.
COMPLAINT = re.compile(r"kiinni|suljettu|väärin|vanhat|eri ajat|tauolla|stängt|fel öppettider|gamla tider|"
                       r"stengt|feil åpningstider|closed|wrong hours|old hours|different hours", re.I)
CLOSED_FOR_GOOD = re.compile(r"pysyvästi|permanent|lopettanut|stängt för gott|nedlagt|closed down", re.I)


def load_complaints():
    """{entity_id: [review dates]} for complaint reviews about closed doors or wrong hours."""
    import json
    from datetime import datetime, timezone
    with open(os.path.join(MAT, "data", "reviews.json"), encoding="utf-8") as f:
        reviews = json.load(f)["response"]["reviews"]
    out = {}
    for r in reviews:
        text = r.get("content") or ""
        if (r["locationId"] in TARGETS and text and (r.get("rating") or 5) <= REVIEW_MAX_STARS
                and COMPLAINT.search(text) and not CLOSED_FOR_GOOD.search(text)):
            when = datetime.fromtimestamp(r["publisherDate"] / 1000, timezone.utc).date()
            out.setdefault(r["locationId"], []).append((when, text))
    return out


COMPLAINTS = load_complaints()


def relevant_complaint(eid, day):
    """A complaint review written around the same date in an earlier year (e.g. last Christmas), or None."""
    for when, text in COMPLAINTS.get(eid, []):
        for back in (1, 2):
            try:
                same = day.replace(year=day.year - back)
            except ValueError:      # 29 Feb
                continue
            if abs((when - same).days) <= REVIEW_NEAR_DAYS:
                return when, text
    return None


def urgent_reason(items):
    """Why a message is urgent enough for an AI call, or "" if it isn't (team decision).
    Urgent = on some day in the message Google shows the wrong open/closed state (open when we think it's
    closed, or the other way round), AND at least one of:
      - that day got more searches than usual last year (at least 2x a normal day), or
      - a bad review around the same date in an earlier year says it was closed or the hours were wrong."""
    for s in items:
        if (s["shown"] == "closed") == (s["hours"] == "closed"):
            continue
        wrong = (f"Google shows {'closed' if s['shown'] == 'closed' else 'open'} on {s['weekday'][:3]} {s['dm']}, "
                 f"we think it's {'closed' if s['hours'] == 'closed' else 'open'}")
        spike = SPIKES.get(f"{s['eid']}|{s['day'].isoformat()}", 0)
        if spike >= SPIKE_URGENT:
            return f"{wrong}; last year that day got {spike:g}× the usual searches"
        rev = relevant_complaint(s["eid"], s["day"])
        if rev:
            return f"{wrong}; bad review {rev[0].day} {rev[0]:%b %Y}: \"{rev[1][:70]}{'…' if len(rev[1]) > 70 else ''}\""
    return ""


def contact_timeline(eid, items):
    """[(when, what)] for one message: first message, reminder, and AI calls only when urgent.
    Each step only happens if the owner hasn't replied yet."""
    from datetime import datetime, timedelta
    if items[0].get('live_check'):
        return [(None, 'One owner check in the contact window; no reminder or voice escalation')]
    if items[0].get('check'):
        chk = items[0]['check']    # weather is short notice: WhatsApp, where half of owners reply within 40 min
        ch = "WhatsApp" if chk['kind'] == 'weather' else LABEL.get(LEARN.channel(eid)[0], 'WhatsApp')
        return [(None, f"{ch} · " + chk['timeline'])]
    try:
        pol = S5.policy(eid)
    except Exception:
        pol = {}
    first_ch = LEARN.channel(eid)[0]     # preferred channel at first, then learned from replies (learning.py)
    hour = send_hour(pol.get("best_contact_window"))
    day = min(s["day"] for s in items)
    send = datetime(day.year, day.month, day.day, hour) - timedelta(weeks=SEND_WEEKS_BEFORE)
    remind = send + timedelta(days=REMINDER_AFTER_DAYS.get(first_ch, 1))
    steps = [(send, f"{LABEL.get(first_ch, first_ch)} message"), (remind, "WhatsApp reminder")]
    if not urgent_reason(items):
        return steps + [(None, "stop (not urgent, no call)")]
    call = remind + timedelta(days=CALL_AFTER_REMINDER_DAYS)
    deadline = datetime(day.year, day.month, day.day, hour) - timedelta(days=CALL_LATEST_DAYS_BEFORE)
    if call > deadline:            # message went out late (e.g. an event found late): call the day after the reminder
        call = max(remind + timedelta(days=1), datetime.now().replace(minute=0, second=0, microsecond=0))
    return steps + [(call, "AI voice call (urgent)"), (call + timedelta(days=1), "Second call, then stop")]


def timeline_text(steps):
    return " → ".join(f"{w:%a} {w.day} {w:%b} {w:%H:%M} {what}" if w else what for w, what in steps)


def connect_index():
    rows = []
    for eid in sorted(set(DEMO) | {eid for eid, _ in LIVE.rows}):
        ent = BL.ents[eid]
        try:
            pref = S5.policy(eid).get("preferred_channel")
        except Exception:
            pref = None
        nxt = LEARN.channel(eid)[0]     # where the next message goes: preferred first, then learned from replies
        msgs = []
        plan = season_plan(eid)
        for pid, title, items in bundles_for(eid):
            d = DECISIONS.get((eid, pid))
            n = len(items)
            k = ("w", eid, pid)
            answered = any((c, eid, pid) in WA for c in ("w", "s", "e", "p"))
            send, why = plan.get(pid, (True, ""))
            if not send and not answered:
                msgs.append(f"<div style='margin-bottom:6px' class='muted'><s>{h(title)} · {n} day{'s' if n > 1 else ''}</s>"
                            f" <span class='tag'>Not sent: {h(why)}</span></div>")
                continue
            if answered:
                rem = ""
            elif k in NOREPLY:
                rem = " <span class='tag'>No reply (learned)</span>"
            elif k in REMIND:
                rem = (" <span class='tag'>Reminder sent</span>"
                       f" <form method='post' action='/w/{eid}/{pid}/noreply' style='display:inline'>"
                       f"<button class='tag' style='cursor:pointer' title='Still no reply after the reminder'>No reply</button></form>")
            else:
                rem = (f" <form method='post' action='/w/{eid}/{pid}/remind' style='display:inline'>"
                       f"<button class='tag' style='cursor:pointer' title='No reply after 24 h'>Send reminder</button></form>")
            links = " · ".join(f"<a href='/{c}/{eid}/{pid}'>" + (f"<b>★ {LABEL[api]}</b>" if api == nxt else LABEL[api]) + "</a>"
                               for c, api in (("w", "WHATSAPP"), ("s", "SMS"), ("e", "EMAIL"), ("p", "APP_PUSH")))
            if items[0].get('live_check'):
                rem = ''
                row = items[0]['live_check']
                source = row['event']['x_provenance']['provider']
                links += f" <span class='tag'>{h(LIVE.mode)} · {h(source)}</span>"
                url = row['event'].get('info_url', {}).get('en', '')
                if urllib.parse.urlparse(url).scheme in ('http', 'https'):
                    links += f" · <a href='{h(url)}' target='_blank' rel='noopener'>Event source</a>"
            if items[0].get('check'):
                chk = items[0]['check']
                if chk['kind'] == 'weather':
                    rem = ''           # short notice: no reminder (the season check keeps its reminder)
                links = (f"<a href='/w/{eid}/{pid}'>WhatsApp</a> <span class='tag'>{'Weather' if chk['kind'] == 'weather' else 'Season'}"
                         f" check · {h(chk['why'])}</span>"
                         + (f" <span class='tag'>{h(chk['source'])}</span>" if chk.get('source') else ''))
            msgs.append(f"<div style='margin-bottom:8px'>{h(title)} · {n} day{'s' if n > 1 else ''}: "
                        f"{links} · <a href='/b/{eid}/{pid}'>Web page</a>{rem}"
                        f"{' <span class=tag>' + h(d) + '</span>' if d else ''}"
                        f"{' <span class=tag>' + h(why) + '</span>' if why.startswith('Quiet') and not answered else ''}"
                        f"<br><span class='muted' style='font-size:13px'>{h(timeline_text(contact_timeline(eid, items)))}"
                        + f"<br>{h(delivery_status(eid, pid))}"
                        + (f"<br>{h(live_weather_text(items[0]['live_check']))}" if items[0].get('live_check') else '')
                        + (f"<br>Urgent: {h(urgent_reason(items))}" if urgent_reason(items) else "")
                        + "</span></div>")
        for (e2, _), (t2, gone, left) in sorted(IG_REPLACED.items()):
            if e2 == eid:
                msgs.append(f"<div class='muted' style='margin-bottom:6px;font-size:13px'>{h(t2)}: {gone} day"
                            f"{'s' if gone > 1 else ''} left out, already in <a href='/ig/{eid}'>the Instagram message</a>"
                            f"{'' if left else ' (no separate holiday message)'}</div>")
        if opted_out(eid):
            msgs = ["<span class='muted'>Opted out of suggestions: no message</span>"]
        if not msgs:
            first = suggestion(eid, TARGETS[eid][0])
            msgs = ["<span class='muted'>" + ("Flag for a person: may have closed for good" if first["action"] == "FLAG"
                                              else "No message: hours already right") + "</span>"]
        prefers = f"<br><span class='muted'>Prefers {LABEL[pref]}</span>" if pref in LABEL else ""
        rows.append(f"<tr><td><b>{h(ent['name'])}</b><br><span class='muted'>{h(eid)} · {h(ent['address']['city'])}</span>"
                    f"{prefers}<br><a href='/w/{eid}/welcome'>Welcome message</a>"
                    f" · <form method='post' action='/learning/{eid}/silent' style='display:inline'>"
                    f"<button class='tag' style='cursor:pointer' title='Demo: one more unanswered message (after its reminder)'>"
                    f"Simulate: no reply</button></form>"
                    f"<br><span class='muted' style='font-size:12px'>{h(LEARN.summary(eid))}</span></td>"
                    f"<td>{''.join(msgs)}</td></tr>")
    target = ("real Yext sandbox" if S5.applier.primary.name == 'sandbox' else
              "local Yext mock and configured Yext sandbox" if S5.applier.sandbox else "local Yext mock")
    body = (f"<div class='brand'><span class='dot'></span>Connect · demo control panel</div>"
            f"<div class='card'><h1>Messages ready to send</h1>"
            f"<p class='muted'>Browser demo ready. Showing {len(DEMO)} challenge businesses available for this target. Suggestions are grouped into one message per "
            f"holiday period (Christmas 21–30 Dec and New Year 31 Dec–8 Jan, including school-holiday days; every other holiday, special day or local event on its own). They come from the "
            f"team's C13 engine{'' if ENGINE else ' (output missing: challenge baseline)'}. Approved changes are written to the {target}.</p>"
            + ai_status_line() +
            f"<p class='muted'><b>Apply target:</b> {h(target)}. {h(S5.selection_reason)}. Browser channels are previews; this page sends no real messages.</p>"
            f"<p class='muted'>Yext sandbox write/read-back status is shown per message below. Publisher timings are estimates from the challenge data; they do not verify publication on Google or other sites.</p>"
            + (f"<p class='muted'><b>Live hours checks:</b> {len(LIVE.rows)} selected checks using real event listings and synthetic businesses. "
               f"Mode: {h(LIVE.mode)}. Current hours are offered for owner confirmation; the event does not predict new hours. "
               f"Checks are limited to one per business in 14 days, with no reminders or calls.</p>" if LIVE.rows else '') +
            f"<p class='muted'>Each message can be opened as WhatsApp, SMS, email or app push. <b>★</b> marks the proposed channel: "
            f"the owner's preferred channel (from <code>customer_contacts.csv</code>) at first, then learned from replies. "
            f"No reply → the next message tries WhatsApp, then SMS (at most 3 channels; a new customer moves on after 1 "
            f"unanswered message, others after 2). Quiet mode only once every channel was tried.</p>"
            f"<p class='muted'><b>When:</b> about 3 weeks before the first day in the message, at the start of the "
            f"owner's contact window but never after 17:00. No reply → WhatsApp reminder after 1 day (2 days after an "
            f"email) → only if urgent: AI voice call 2 days later and one more call → stop and change nothing. "
            f"Each step only happens if the owner still hasn't replied.</p>"
            f"<p class='muted'>Under each business: what Connect has learned about that owner (yes, no, no reply, UNDO). "
            f"Messages it wouldn't send are crossed out. <a href='/learning/reset'>Reset learning</a></p>"
            f"<table><tr><th>Business</th><th>Messages (open the owner's phone)</th></tr>{''.join(rows)}</table></div>"
            f"{posts_card()}{ig_card()}")
    return page("Connect demo", body, wide=True)


VIA = ("WEB", "SMS", "EMAIL", "APP_PUSH")   # where the owner came from to the web page (sent to step 5)


def bundle_page(eid, pid, title, items, form=None, bad=None, via="WEB"):
    """Contact plan step 6: all dates of one holiday period in one message, approved with one tap.
    Also the page behind the SMS link, the email's "Other hours" button and the app push (inside the app)."""
    form, bad = form or {}, bad or set()
    via = form.get("via", via) if form.get("via", via) in VIA else "WEB"
    c0 = items[0]
    live = bool(c0.get('live_check') or (c0.get('check') or {}).get('confirm'))
    if live:
        seen_for(eid, pid, items)
    days = []
    for s in items:
        k = s["day"].isoformat()
        choice = form.get(f"c_{k}", "sug")
        first = s["hours"].split(",")[0] if s["hours"] != "closed" else s["shown"].split(",")[0]
        o, cl = first.split("-") if first != "closed" else ("10:00", "16:00")
        o, cl = form.get(f"o_{k}", o), form.get(f"e_{k}", cl)

        def radio(val, label):
            return (f"<label class='ch'><input type='radio' name='c_{k}' value='{val}' "
                    f"{'checked' if choice == val else ''}>{label}</label>")
        days.append(
            f"<div class='day{' bad' if k in bad else ''}'><div class='when'>{s['weekday'][:3]} {h(s['dm'])} · {h(s['day_name'])}</div>" +
            (f"<div class='hrs'>Listed hours: <b>{h(state(s['shown'], 'en'))}</b></div>" if live else
             f"<div class='hrs'><s>{h(state(s['shown'], 'en'))}</s> → <b>{h(state(s['hours'], 'en'))}</b></div>") +
            f"{'<div class=why>' + h(s['short']) + '</div>' if s['short'] else ''}"
            f"<details{' open' if choice != 'sug' or k in bad else ''}><summary>Change this day</summary>"
            f"{radio('sug', 'Confirm listed hours' if live else 'Use the suggestion')}"
            f"{radio('custom', 'Different hours')}"
            f"<div class='times'><input type='time' name='o_{k}' value='{h(o)}' aria-label='Opens'>"
            f"<input type='time' name='e_{k}' value='{h(cl)}' aria-label='Closes'></div>"
            f"{radio('closed', 'Closed all day')}"
            f"{radio('skip', 'Leave as it is')}"
            f"{'<p class=err>Closing time has to be after opening time.</p>' if k in bad else ''}"
            f"</details></div>")
    n = len(items)
    intro_txt = f"{intro(pid, title, items)} {proposal_context(items)} Here's what we suggest:"
    if live:
        intro_txt = intro(pid, title, items) + ' Are your listed hours right for that day?'
    if c0.get('check'):
        intro_txt = c0['check']['intro'] + ' ' + c0['check']['question']
    yearly = '' if live or c0.get('check') else (f"<label class='opt'><input type='checkbox' name='always' value='1' "
                              f"{'checked' if form.get('always') else ''}>{UI['en']['always']}</label>")
    body = (f"<div class='brand'><span class='dot'></span>Fonecta Connect</div><div class='card'>"
            f"<h1>Hi {h(c0['name'])}!</h1><p>{h(intro_txt)}</p>"
            f"<form method='post' action='/b/{eid}/{pid}/approve'><input type='hidden' name='via' value='{via}'>{''.join(days)}"
            f"{yearly}<button class='btn primary' type='submit'>{'Confirm hours' if live else 'Approve'}{' all ' + str(n) + ' days' if n > 1 else ''}</button></form>"
            f"<form method='post' action='/b/{eid}/{pid}/decline'><input type='hidden' name='via' value='{via}'>"
            f"<button class='btn plain' type='submit'>No thanks</button></form>"
            f"<p class='muted' style='margin:14px 0 0'>{UI['en']['safe']}</p></div>")
    return page(c0["name"], body)


WA = {}        # (channel, eid, pid) -> "yes" | "no" | "other" | "stop": the owner's answer (w/s/e/p)
WA_CHAT = {}   # (channel, eid, pid) -> [("me" | "them" | "ask", text)]: replies after the first answer
WA_AUTO = {}   # (channel, eid, pid) -> {"state": "asked" | "yes" | "no", "grants": [(day_key, hours)]}

# Asked once, right after "Yes, update all": may Connect handle these days for the owner from now on?
# A yes is saved as a standing permission in step 5 (only for these exact days and hours). Next year step 5's
# /policy says auto_apply, so Connect sets the same hours and then tells the owner (with UNDO), instead of asking.
AUTO_YES, AUTO_NO = "Yes, handle it", "No, ask me first"   # quick-reply buttons: max 20 characters
ASK_ME_WORDS = {"ask me", "ask me first", "stop auto"}


def auto_question(title, pid):
    what = f"your {title} hours" if pid in ("christmas", "new-year") else "these hours"
    return (f"One more thing: shall we handle {what} for you next year?\n\n"
            "We'd set the same hours automatically and send you a message when it's done. "
            "You can change your mind anytime by replying ASK ME.")


def auto_grants(eid, items, res):
    """(day_key, hours) for the days step 5 applied (or that were already right) and that repeat every year."""
    ok = {it["date"] for it in res.get("items", []) if it["status"] in ("APPLIED", "ALREADY_SET")}
    out = []
    for x in items:
        if x.get('live_check') or x.get('check'):     # one-off checks never become yearly permissions
            continue
        k = x["day"].isoformat()
        key = SD.day_key(eid, k)
        if k in ok and key not in SD.NOT_YEARLY:
            out.append((key, x["hours"]))
    return out


def already_handled(eid, grants):
    """Skip the question if the owner already gave permission (in the CRM, or for exactly these hours)."""
    return all(S5.policy(eid, day_key=key, hours=hrs)["auto_apply"] for key, hrs in grants)


def revoke_auto(k, eid):
    """Owner took back "handle it for me" (ASK ME, or UNDO because the hours were wrong)."""
    auto = WA_AUTO.get(k)
    if not auto or auto["state"] != "yes":
        return False
    for key, _ in auto["grants"]:
        S5.applier.permissions.revoke(eid, key)
    S5.applier.audit.append({"type": "PERMISSION_REVOKED", "entity_id": eid, "actor": "owner",
                             "channel": "WHATSAPP", "days": [g[0] for g in auto["grants"]]})
    auto["state"] = "no"
    return True
WA_DONE = {}   # (channel, eid, pid) -> True once the owner's own hours are saved
REMIND = set()  # (channel, eid, pid) -> the reminder was sent (no reply after 24 h)

# ---------------------------------------------------------------- learning what each owner wants
# docs/new-customer-plan.md: a welcome message (no questions), then a score per reason (public holiday,
# local event, ...) learned from yes / no / no reply / UNDO. Quiet mode after 3 unanswered messages; STOP.
LEARN = learning.Learner(os.path.join(DEMO_SESSION.name, 'learning.json') if DEMO_MODE else
                         os.path.join(os.environ['CONNECT_RUNTIME_DIR'], 'learning.json')
                         if os.environ.get('CONNECT_RUNTIME_DIR') else learning.STATE,
                         preferred=lambda eid: _preferred(eid))


def _preferred(eid):
    try:
        return S5.policy(eid).get("preferred_channel")
    except Exception:
        return None


NOREPLY = set()       # (channel, eid, pid) -> counted as "no reply" (after the reminder)
EDIT_LEARNED = set()  # (channel, eid, pid) -> typed hours already counted once
WELCOME_CHAT = {}     # eid -> [("me" | "them", text)] replies to the welcome message
STOP_WORDS, START_WORDS = {"stop", "stopp", "lopeta"}, {"start", "aloita"}


WA_MORE = {}         # (channel, eid, pid) -> {"state": "asked" | "yes" | "no", "reasons": [...]}
MORE_YES, MORE_NO = "Yes, keep sending", "No, not these"   # quick-reply buttons: max 20 characters
ABOUT = {"holiday": "public holidays", "eve": "holiday eves", "theme": "special days like Black Friday",
         "event": "local events", "school": "school holidays", "weather": "the weather"}


def about(reasons):
    names = [ABOUT[r] for r in reasons]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def more_question(reasons):
    """Asked after a "no", once per reason: did they dislike this one suggestion, or this kind?"""
    return f"Would you like suggestions about {about(reasons)} in the future?"


def day_keys(items):
    return [x["key"] for x in items]


def season_plan(eid):
    """Which of this season's messages Connect would send to this owner, from what it has learned."""
    msgs = [{"id": pid, "days": [(x["day"], x["key"]) for x in items],
             "wrong": any(x["hours"] != x["shown"] for x in items)} for pid, _, items in bundles_for(eid)]
    plan = LEARN.plan(eid, msgs)
    for pid, _, items in bundles_for(eid):
        if items[0].get('live_check'):
            reason = live_block(eid, pid)
            plan[pid] = (not reason, reason)
    return plan


def first_name(eid):
    return S5.applier.contacts.get(eid, {}).get("contact_first_name") or BL.ents[eid]["name"]


def stop_start_reply(eid, text):
    """STOP / START work in every chat. Returns Connect's answer, or None for other text."""
    t = text.lower().strip(" .!")
    if t in STOP_WORDS:
        LEARN.set_stopped(eid, True)
        return ("OK, we'll stop sending suggestions.\n\nWe'll only message you if something looks clearly wrong, "
                "for example if Google shows you open when you may have closed.\n\nReply START to turn suggestions back on.")
    if t in START_WORDS:
        LEARN.set_stopped(eid, False)
        return "Welcome back! Suggestions are on again. Nothing changes without your permission."
    return None


def welcome_text(eid):
    return (f"Hi {first_name(eid)}! 👋 This is Fonecta Connect.\n\n"
            "Now and then I'll send you short suggestions to help customers find you, like holiday hours or nearby events.\n\n"
            "Nothing changes without your permission, and you can always reply UNDO.")


def welcome_page(eid):
    """The first message a new customer gets: a friendly hello, no questions (we learn from their answers later)."""
    chat = (f"<div class='b'>{h(welcome_text(eid))}<span class='ft'>We never ask for logins or passwords.</span>"
            f"<span class='t'>09:00</span></div>")
    for who, text in WELCOME_CHAT.get(eid, []):
        chat += f"<div class='b{' me' if who == 'me' else ''}'>{h(text)}<span class='t'>09:1{'4 ✓✓' if who == 'me' else '4'}</span></div>"
    chat += (f"<form method='post' action='/w/{eid}/welcome/reply' class='wa-in'>"
             f"<input name='msg' placeholder='Message' autocomplete='off' required aria-label='Reply'>"
             f"<button aria-label='Send'>➤</button></form>")
    top = ("<div class='wa-top'><div class='wa-av'>FC</div><div><b>Fonecta Connect</b>"
           "<small>Business account</small></div></div>")
    body = (f"<div class='wa'>{top}<div class='wa-body'><div class='wa-day'><span>Welcome · first message</span></div>"
            f"{chat}<div id='end'></div></div></div><p class='muted' style='text-align:center'><a href='/'>← Connect demo panel</a> · "
            f"<a href='/w/{eid}/welcome/reset'>Reset this chat</a></p>")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>WhatsApp · {h(BL.ents[eid]['name'])}</title>"
            f"<style>{CSS}{WA_CSS}body{{background:#d1d7db}}</style></head><body>{body}</body></html>")

# Channels: w = WhatsApp, s = SMS, e = email, p = app push. (No Messenger: businesses can't start chats there.)
CH_API = {"w": "WHATSAPP", "s": "SMS", "e": "EMAIL", "p": "APP_PUSH"}
LABEL = {"WHATSAPP": "WhatsApp", "SMS": "SMS", "EMAIL": "Email",
         "APP_PUSH": "App push", "WEB": "Web page"}
NO_TEXT = "OK, we won't change anything. Your existing hours stay unchanged."

# Team decision: a polite reply ("thanks", "kiitos", "ok", "tack") gets a polite answer, not "I didn't catch that".
POLITE = {"thanks", "thank you", "thanks a lot", "thx", "ty", "kiitos", "kiitti", "kiitos paljon", "tack", "tack så mycket",
          "takk", "tusen takk", "ok", "okay", "okei", "oki", "selvä", "hyvä", "great", "perfect", "good", "bra", "fint",
          "👍", "🙏", "😊"}
POLITE_REPLY = "You're welcome! If anything changes, just reply here."


def is_polite(text):
    t = re.sub(r"[\s.!,:)]+$", "", text.strip().lower())
    t = re.sub(r"\s*[👍🙏😊🙂❤️]+$", "", t).strip() or t
    return t in POLITE or text.strip() in POLITE


def split_known(items):
    """(days with last year's hours, days without history)."""
    return [s for s in items if s["last_hours"]], [s for s in items if not s["last_hours"]]


def question_for(pid, title, items):
    if items[0].get('check'):
        return items[0]['check']['question']
    if items[0].get('live_check'):
        return 'Are your listed hours right for that day?'
    known, new = split_known(items)
    if known and not new:
        return "Shall we use the same hours this year?"
    if pid in ("christmas", "new-year"):
        return f"Shall we update your {title} hours?"
    return f"Shall we update your hours for {items[0]['weekday'][:3]} {items[0]['dm']}?"


def plain(hrs):
    """Hours with a normal hyphen, so an SMS stays in the cheap GSM alphabet."""
    return "closed" if hrs == "closed" else nice(hrs).replace("–", "-")


def compact(days, sep=", "):
    """[(date, hours)] -> '24 Dec 10-14, 25-26 Dec closed'. Days in a row with the same hours are joined."""
    runs = []
    for d, hrs in sorted(days):
        if runs and runs[-1][2] == hrs and (d - runs[-1][1]).days == 1:
            runs[-1][1] = d
        else:
            runs.append([d, d, hrs])
    out = []
    for a, b, hrs in runs:
        if a == b:
            when = f"{a.day} {a:%b}"
        elif a.month == b.month:
            when = f"{a.day}-{b.day} {b:%b}"
        else:
            when = f"{a.day} {a:%b}-{b.day} {b:%b}"
        out.append(f"{when} {plain(hrs)}")
    return sep.join(out)


def summary(items):
    """Short 'Last year you were open: … / We suggest: …' line for SMS, email preview and app push."""
    if items[0].get('live_check'):
        return 'Listed hours: ' + compact([(s['day'], s['shown']) for s in items]) + '.'
    known, new = split_known(items)
    parts = []
    if known:
        parts.append("Last year you were open: " + compact([(s["day"], s["last_hours"]) for s in known]) + ".")
    if new:
        parts.append(("For the other days we suggest: " if known else "We suggest: ")
                     + compact([(s["day"], s["hours"]) for s in new]) + ".")
    return " ".join(parts)


AI_STATUS = {"state": "checking"}


def watch_ai():
    """Every 2 minutes: does the OpenShift AI model answer? Shown on the panel, so nobody demos without it."""
    while True:
        AI_STATUS["state"] = hours_ai.status()
        time.sleep(120)


def ai_status_line():
    return f"<p class='muted'><b>AI reply reader (Red Hat OpenShift AI):</b> {h(hours_ai.status())}</p>"


def wa_answer(eid, pid, b, ans, typed=False):
    """WhatsApp Yes / No / Other hours, from a quick-reply button or typed as a word. Yes/No go to step 5."""
    ch, k = "w", ("w", eid, pid)
    WA[k] = ans
    items = b[2]
    yes = "Yes, update all" if len(items) > 1 else "Yes, update"
    chat = WA_CHAT.setdefault(k, [])
    labels = {'yes': 'Yes, confirm hours', 'no': 'Not relevant', 'other': 'Other hours'} if items[0].get('live_check') else \
        items[0]['check']['labels'] if items[0].get('check') else \
        {"yes": yes, "no": "No, keep as it is", "other": "Other hours"}
    if not typed:                         # a typed "yes" is already in the chat as the owner wrote it
        chat.append(("me", labels[ans]))
    if ans == "yes":
        res = apply_via_step5(eid, pid, "WHATSAPP", [(x, "ACCEPTED", x["hours"]) for x in items])
        chat.append(("them", without_where(res["message"], eid) or
                     "Your hours are already up to date. Nothing needed changing."))
        DECISIONS[(eid, pid)] = f"WhatsApp: yes, {len(res['applied'])} applied by step 5"
        if not items[0].get('live_check') or any(r['status'] in ('APPLIED', 'ALREADY_SET') for r in res['items']):
            LEARN.record(eid, day_keys(items), "YES", pid, "WHATSAPP")
        grants = auto_grants(eid, items, res)
        if grants and not already_handled(eid, grants):
            WA_AUTO[k] = {"state": "asked", "grants": grants}
            chat.append(("ask", auto_question(b[1], pid)))
        if not items[0].get('live_check'):      # then: "shall we also post these hours?"
            okd = {r["date"] for r in res["items"] if r["status"] in ("APPLIED", "ALREADY_SET")}
            hours_post(eid, pid, b[1], [(x, x["hours"]) for x in items if x["day"].isoformat() in okd])
    elif ans == "no":
        apply_via_step5(eid, pid, "WHATSAPP", [(x, "REJECTED", None) for x in items])
        chat.append(("them", (items[0]['check']['no_reply'] if items[0].get('check') else
                              "OK, we won't change anything. Your existing hours stay unchanged.")
                     + "\nIf something changes, just reply here."))
        DECISIONS[(eid, pid)] = "WhatsApp: declined"
        LEARN.record(eid, day_keys(items), "NO", pid, "WHATSAPP")
        reasons = LEARN.to_ask(eid, day_keys(items))
        if reasons:
            WA_MORE[k] = {"state": "asked", "reasons": reasons}
            chat.append(("more", more_question(reasons)))
    else:
        ex, ex2 = items[0], (items[1] if len(items) > 1 else None)
        example = f"{ex['day'].day} {ex['day']:%b} 10-14" + (f"\n{ex2['day'].day} {ex2['day']:%b} closed" if ex2 else "")
        chat.append(("them", f"No problem! Which hours would you like? Reply here, one day per line, "
                             f"for example:\n\n{example}\n\nDays you don't mention stay as they are."))
        DECISIONS[(eid, pid)] = "WhatsApp: wants other hours"


def answer(ch, eid, pid, items, ans):
    """Yes / No on any channel -> step 5. Returns step 5's result."""
    label = LABEL[CH_API[ch]]
    if ans == "yes":
        res = apply_via_step5(eid, pid, CH_API[ch], [(x, "ACCEPTED", x["hours"]) for x in items])
        DECISIONS[(eid, pid)] = f"{label}: yes, {len(res['applied'])} applied by step 5"
        LEARN.record(eid, day_keys(items), "YES", pid, CH_API[ch])      # learning per owner counts every channel
        return res
    res = apply_via_step5(eid, pid, CH_API[ch], [(x, "REJECTED", None) for x in items])
    DECISIONS[(eid, pid)] = f"{label}: declined"
    LEARN.record(eid, day_keys(items), "NO", pid, CH_API[ch])
    return res


TYPED_YES = ("yes", "y", "ok", "okay", "yes please", "yes update", "yes, update", "yes update all", "yes, update all", "sure")
TYPED_NO = ("no", "n", "no thanks", "no, thanks", "keep it", "no keep as it is", "no, keep as it is")


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_hours_pattern(text, items):
    """Read an owner's free-text reply like '24 Dec 10-14' / '25.12 closed' into {date: hours}.
    Returns (found, problems). Only days in this message's holiday period are accepted."""
    import re
    by_day = {(s["day"].day, s["day"].month): s for s in items}
    found, problems = {}, []
    for chunk in re.split(r"[\n,;]+", text.lower()):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"(\d{1,2})\s*(?:\.|th|st|nd|rd)?\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?", chunk)
        if m:
            dm = (int(m.group(1)), MONTHS[m.group(2)])
        else:
            m = re.match(r"(\d{1,2})[./](\d{1,2})\b\.?", chunk)
            dm = (int(m.group(1)), int(m.group(2))) if m else None
        rest = chunk[m.end():] if m else chunk
        if dm is None:
            if len(items) == 1:
                dm = (items[0]["day"].day, items[0]["day"].month)
            else:
                problems.append(chunk)
                continue
        s = by_day.get(dm)
        if s is None:
            problems.append(chunk)
            continue
        if re.fullmatch(r"\s*(?:closed|shut)\s*[.!]?", rest):
            if s['day'] in found:
                problems.append(chunk)
            found[s["day"]] = "closed"
            continue
        t = re.fullmatch(r"\s*(\d{1,2})(?:[:.](\d{2}))?\s*(?:-|–|to)\s*(\d{1,2})(?:[:.](\d{2}))?\s*", rest)
        if not t:
            problems.append(chunk)
            continue
        o = f"{int(t.group(1)):02d}:{t.group(2) or '00'}"
        c = f"{int(t.group(3)):02d}:{t.group(4) or '00'}"
        if H.validate(f"{o}-{c}") or s['day'] in found:
            problems.append(chunk)
            continue
        found[s["day"]] = f"{o}-{c}"
    return ({}, problems) if problems else (found, [])


def browser_result(result):
    """Keep demo product copy natural; scope remains visible in the operator panel."""
    if result is not None and result.get("apply_target") == "demo":
        return {**result, "message": result.get("owner_message", result.get("message"))}
    return result


def initialize_messaging():
    """Missing/broken setup must not prevent the credential-free browser demo."""
    cfg = messaging.configuration()
    if DEMO_MODE:
        return None, {**cfg, 'mode': 'demo', 'reason': 'Fresh browser demo; external delivery disabled'}
    if not cfg["configured"]:
        return None, {**cfg, "mode": "demo", "reason": "messaging configuration unavailable"}
    try:
        service = messaging.from_env(messaging_proposal, messaging_hours)
    except (ValueError, RuntimeError, ImportError, OSError):
        return None, {**cfg, "mode": "demo", "reason": "messaging setup unavailable; browser demo remains ready"}
    return service, {**cfg, "mode": "real", "reason": "real transport configured; delivery requires provider confirmation"}


def proposal_context(items):
    if all(not s.get('has_exception') for s in items):
        return "No special hours are saved for these dates yet."
    return "Please review these suggested changes to your saved special hours."


def no_pending_proposal(eid):
    return page(BL.ents[eid]['name'], "<div class='brand'><span class='dot'></span>Fonecta Connect</div>"
                "<div class='card'><span class='tag'>No pending proposal</span><h1 style='margin-top:14px'>No update needed</h1>"
                f"<p>{h(BL.ents[eid]['name'])} has no pending suggested changes for this period.</p>"
                "<p class='muted'>Your saved hours are kept as they are. Resetting a conversation doesn't undo an approved update.</p>"
                "<a class='btn primary' href='/'>Back to businesses</a></div>")


def messaging_proposal(eid, pid, source=None):
    """Freeze the selected proposal as text and data for a real WhatsApp template."""
    b = next((b for b in bundles_for(eid, source=source) if b[0] == pid), None) if eid in BL.ents else None
    if not b:
        raise messaging.MessagingError("No eligible suggestion for this business/period")
    _, title, items = b
    known = [s for s in items if s["last_hours"]]
    new = [s for s in items if not s["last_hours"]]
    parts = [intro(pid, title, items), proposal_context(items)]
    def line(s, hours):
        return f"{s['weekday'][:3]} {s['dm']} ({s['day_name']}): " + ("closed" if hours == "closed" else nice(hours))
    if known:
        parts.append("Last year you were open: " + "; ".join(line(s, s["last_hours"]) for s in known) + ". We'd use the same hours this year.")
    if new:
        parts.append(("For the other days we suggest: " if known else "We suggest: ") + "; ".join(line(s, s["hours"]) for s in new))
    question = "Shall we use the same hours this year?" if known and not new else f"Shall we update your {title} hours?"
    detail = " ".join(parts)
    # Template variables cannot contain newlines; fail instead of silently truncating proposed dates.
    if len(detail) > 1200 or "\n" in detail:
        raise messaging.MessagingError("Proposal is too long for the approved template; split/review it before sending")
    return {"name": items[0]["name"], "detail": detail, "question": question,
            "items": [{"date": s["day"].isoformat(), "hours": s["hours"], 'day_name': s['day_name']} for s in items]}


def messaging_hours(text, frozen_items):
    found, problems = parse_hours_reply(text, [{"day": date.fromisoformat(x["date"]), 'day_name': x.get('day_name', '')} for x in frozen_items])
    return {d.isoformat(): hrs for d, hrs in found.items()}, problems


def parse_hours_reply(text, items):
    if hours_ai.configured():
        return hours_ai.read_reply(text, items)
    return parse_hours_pattern(text, items)

WA_CSS = """
.wa{max-width:420px;margin:0 auto;min-height:100vh;background:#efe7de;font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;color:#111b21}
.wa-top{background:#075e54;color:#fff;padding:12px 14px;display:flex;align-items:center;gap:10px}
.wa-av{width:36px;height:36px;border-radius:50%;background:#fff;color:#14198a;font-weight:700;display:flex;align-items:center;justify-content:center;font-size:13px}
.wa-top b{display:block;font-size:16px}.wa-top small{opacity:.85;font-size:12px}
.wa-body{padding:12px 10px 24px}
.wa-day{text-align:center;margin:4px 0 10px}.wa-day span{background:#fff;border-radius:8px;padding:3px 10px;font-size:12px;color:#54656f}
.b{background:#fff;border-radius:10px;padding:8px 10px 6px;margin:6px 0;max-width:88%;box-shadow:0 1px 0 rgba(0,0,0,.08);white-space:pre-line}
.b.me{background:#d9fdd3;margin-left:auto}
.t{display:block;text-align:right;font-size:11px;color:#667781;margin-top:2px}
.tpl{max-width:88%;margin:6px 0}.tpl .b{max-width:100%;margin:0}
.ft{display:block;color:#667781;font-size:13px;margin-top:6px}
.qb{display:flex;align-items:center;justify-content:center;gap:6px;width:100%;margin-top:3px;padding:10px;border:0;
 border-radius:10px;background:#fff;color:#027eb5;font:inherit;font-weight:500;cursor:pointer;box-shadow:0 1px 0 rgba(0,0,0,.08)}
.qb:disabled{color:#8696a0;cursor:default}.qb.on:disabled{color:#027eb5}
.poll b{display:block;margin-bottom:2px}.poll small{color:#667781;font-size:12px}
.po{display:flex;align-items:center;gap:10px;width:100%;background:none;border:0;border-top:1px solid #e9edef;padding:11px 2px;font:inherit;color:#111b21;cursor:pointer;text-align:left}
.po:first-of-type{border-top:0}
.po i{width:20px;height:20px;border-radius:50%;border:2px solid #8696a0;flex:0 0 20px}
.po.on i{background:#00a884;border-color:#00a884}
.po:disabled{cursor:default}
.b a{color:#027eb5}
.wa-in{display:flex;gap:8px;margin-top:14px}
.wa-in input{flex:1;border:0;border-radius:22px;padding:11px 16px;font:inherit;background:#fff;color:#111b21}
.qr{display:flex;flex-wrap:wrap;gap:8px;justify-content:flex-end;margin:10px 0}
.qr button{border:1px solid #0084ff;color:#0084ff;background:#fff;border-radius:18px;padding:8px 14px;font:inherit;cursor:pointer}
.wa-in button{width:44px;height:44px;border-radius:50%;border:0;background:#00a884;color:#fff;font-size:18px;cursor:pointer}
"""


with open(os.path.join(HERE, 'chat.css'), encoding='utf-8') as style_file:
    WA_CSS += style_file.read()


def wa_page(eid, pid, title, items, ch="w"):
    """Contact plan: the WhatsApp message (a Business template with three quick-reply buttons)."""
    fb = False
    name = items[0]["name"]
    times = CHAT_TIMES.setdefault((ch, eid, pid), {})
    sent_at = times.setdefault('sent', datetime.now().strftime('%H:%M'))
    def bare(hrs):
        return "closed" if hrs == "closed" else nice(hrs)

    # Team decision: if we know last year's hours, show them ("Last year you were open:").
    # Days with no history of their own (or a new business) get "We suggest:".
    known = [s for s in items if s["last_hours"]]
    new = [s for s in items if not s["last_hours"]]
    n = len(items)
    parts = []
    if known:
        parts.append("Last year you were open:\n" + "\n".join(
            f"• {s['day_name']}: {bare(s['last_hours'])}" for s in known))
        if new:
            parts.append("We'd use the same hours this year.")
    if new:
        parts.append(("For the other days we suggest:\n" if known else "We suggest:\n") + "\n".join(
            f"• {s['weekday'][:3]} {s['dm']} ({s['day_name']}): {bare(s['hours'])}" for s in new))
    msg = (f"Hi {name}! This is Fonecta Connect.\n\n"
           f"{intro(pid, title, items)} {proposal_context(items)}\n\n" + "\n\n".join(parts))
    footer = "We never ask for logins or passwords."
    question = question_for(pid, title, items)
    seen_for(eid, pid, items)          # step 5: remember what Yext showed when the message went out
    ans = WA.get((ch, eid, pid))
    # WhatsApp quick-reply buttons allow 20 characters each.
    yes = "Yes, update all" if n > 1 else "Yes, update"
    opts = [("yes", yes), ("no", "No, keep as it is"),
            ("other", "Other hours")]
    if items[0].get('live_check'):
        msg = (f"Hi {name}! This is Fonecta Connect.\n\n{intro(pid, title, items)}\n\n"
               f"Your listed hours: {bare(items[0]['shown'])}.")
        opts = [('yes', 'Yes, confirm hours'), ('no', 'Not relevant'), ('other', 'Other hours')]
    if items[0].get('check'):
        chk = items[0]['check']
        body = chk.get('msg') or chk['intro']
        if chk['kind'] == 'season':
            body += ("\n\nIf you're closed, we'll mark these days closed on Google:\n"
                     + "\n".join(f"• {s['weekday'][:3]} {s['dm']} ({s['day_name']})" for s in items)
                     + f"\n\nWe'll ask about reopening before {chk['reopen'].day} {chk['reopen']:%b}.")
        msg = f"Hi {name}! This is Fonecta Connect.\n\n{body}"
        opts = list(chk['labels'].items())
    # WhatsApp message template: body text + grey footer + three quick-reply buttons under the bubble.
    btns = "" if ans else "".join(f"<button class='qb{' on' if ans == k else ''}' name='a' value='{k}' {'disabled' if ans else ''}>"
                   f"<span aria-hidden='true'>↩</span> {h(label)}</button>" for k, label in opts)
    chat = (f"<form method='post' action='/w/{eid}/{pid}' class='tpl{' answered' if ans else ''}'>"
            f"<div class='b'>{h(msg)}\n\n<b>{h(question)}</b><span class='ft'>{h(footer)}</span>"
            f"<span class='t'>{sent_at}</span></div>{btns}</form>")
    if (ch, eid, pid) in REMIND:
        # Team decision: the "why it matters" number is only in the reminder, sent once after 24 h
        # without a reply (same channel, same three buttons).
        when = f"{title}" if pid in ("christmas", "new-year") else f"{items[0]['weekday'][:3]} {items[0]['dm']}"
        reminder = (f"Hi {name}, just a quick reminder from Fonecta Connect.\n\n"
                    f"{value(eid, pid, title, items)} "
                    + ("It still shows your regular hours every week." if pid == "season" else
                       f"Right now Google still shows your regular hours for {when}."))
        chat += (f"<div class='wa-day'><span>Tomorrow</span></div>"
                 f"<form method='post' action='/w/{eid}/{pid}' class='tpl'>"
                 f"<div class='b'>{h(reminder)}\n\n<b>{h(question)}</b><span class='ft'>{h(footer)}</span>"
                 f"<span class='t'>{sent_at}</span></div>{btns}</form>")
    auto = WA_AUTO.get((ch, eid, pid), {})
    for idx, (who, text) in enumerate(WA_CHAT.get((ch, eid, pid), [])):
        stamp = times.setdefault(idx, datetime.now().strftime('%H:%M'))
        if who == "more":      # "suggestions like this in the future?" with two quick-reply buttons
            more = WA_MORE.get((ch, eid, pid), {})
            btns = "".join(f"<button class='qb{' on' if more.get('state') == k else ''}' name='a' value='{k}' "
                           f"{'disabled' if more.get('state') != 'asked' else ''}><span aria-hidden='true'>↩</span> "
                           f"{h(label)}</button>" for k, label in (("yes", MORE_YES), ("no", MORE_NO)))
            chat += (f"<form method='post' action='/{ch}/{eid}/{pid}/more' class='tpl'><div class='b'>{h(text)}"
                     f"<span class='t'>{stamp}</span></div>{btns}</form>")
            continue
        if who == "ask":       # "handle it for you next year?" with two quick-reply buttons
            done = auto.get("state") != "asked"
            btns = "".join(f"<button class='qb{' on' if auto.get('state') == k else ''}' name='a' value='{k}' "
                           f"{'disabled' if done else ''}><span aria-hidden='true'>↩</span> {h(label)}</button>"
                           for k, label in (("yes", AUTO_YES), ("no", AUTO_NO)))
            chat += (f"<form method='post' action='/{ch}/{eid}/{pid}/auto' class='tpl'><div class='b'>{h(text)}"
                     f"<span class='t'>{stamp}</span></div>{btns}</form>")
            continue
        chat += f"<div class='b{' me' if who == 'me' else ''}'>{h(text)}<span class='t'>{stamp}{' ✓✓' if who == 'me' else ''}</span></div>"
    hp = posts.POSTS.get(f"{eid}~{pid}~hours") if ch == "w" else None
    if hp and auto.get("state") != "asked":        # one question at a time: after "handle it next year?"
        chat += posts.chat_html(hp, name, f"/wp/{urllib.parse.quote(hp['id'])}?back=/w/{eid}/{pid}")
    if (ch, eid, pid) in PENDING_HOURS:
        chat += (f"<form method='post' action='/{ch}/{eid}/{pid}/reply' class='review-actions'>"
                 "<button class='qb' name='msg' value='CONFIRM'>Save these hours</button>"
                 "<button class='qb' name='msg' value='CANCEL'>Cancel</button></form>")
    composer = (f"<form method='post' action='/{ch}/{eid}/{pid}/reply' class='wa-in'>"
                "<textarea name='msg' rows='1' placeholder='Type your reply…' autocomplete='off' required aria-label='Reply'></textarea>"
                "<button aria-label='Send reply'>➤</button></form>")
    top = (f"<div class='wa-top'><a href='/' class='wa-back' aria-label='Back to businesses'>‹</a><div class='wa-av'>FC</div>"
           f"<div><b>Fonecta Connect</b><small>Business account · {h(name)}</small></div></div>")
    ai_ready = all(os.environ.get(k) for k in ('OPENSHIFT_AI_URL', 'OPENSHIFT_AI_TOKEN'))
    ai_title = 'OpenShift AI configured' if ai_ready else 'AI parsing not configured'
    ai_notice = ('Natural-language replies are sent to OpenShift. You review the interpreted hours before saving.' if ai_ready else
                 'Quick replies work. For typed hours, use the exact format below. Natural-language parsing needs OpenShift credentials.')
    if hours_ai.configured() and not ai_ready:
        ai_title = 'AI configuration incomplete'
        ai_notice = 'Quick replies work. Finish setting both OpenShift credentials to enable typed replies, or remove both settings to use the exact-format preview.'
    example = f"{items[0]['dm']} 10-14"
    example2 = f"{items[-1]['dm']} closed"
    target_notice = ('Writes go to the Yext sandbox. Live publication is not verified.' if S5.applier.primary.name == 'sandbox' else
                     'Local simulation. No WhatsApp messages are sent and no public listings are changed.' if not S5.applier.sandbox else
                     'Local simulation with a configured Yext sandbox mirror. No WhatsApp messages are sent.')
    sidebar = ("<aside class='preview-info'><div class='preview-brand'><span class='preview-mark'>FC</span> Fonecta Connect</div>"
               "<p class='preview-eyebrow'>Interactive owner experience</p><h1>Your holiday hours.<br>One conversation.</h1>"
               "<p class='preview-lead'>Approve the suggestion, choose different hours, or leave things as they are. You're in control of every update.</p>"
               f"<div class='preview-status' role='status'><strong>{h(ai_title)}</strong><p>{h(ai_notice)}</p>"
               f"<button type='button' class='preview-example' data-example='{h(example)}'>{h(example)} <span aria-hidden='true'>↗</span></button>"
               f"<button type='button' class='preview-example' data-example='{h(example2)}'>{h(example2)} <span aria-hidden='true'>↗</span></button>"
               "<details><summary>Connect real AI parsing</summary><p>Set <code>OPENSHIFT_AI_URL</code> and <code>OPENSHIFT_AI_TOKEN</code> in <code>.env</code>, then restart. "
               "Verify with <code>python app/app.py --check-services</code>.</p></details>"
               f"<p class='target-status' style='margin-top:14px'>{h(target_notice)}</p></div>"
               f"<nav class='preview-links'><a href='/'>All businesses</a><a href='/{ch}/{eid}/{pid}/reset'>Reset this chat</a><span>Demo preview</span></nav></aside>")
    body = (f"<main class='preview-layout'>{sidebar}<section class='wa{' fb' if fb else ''}' aria-label='WhatsApp conversation'>{top}"
            f"<div class='wa-body' role='log' aria-label='Conversation'><div class='wa-day'><span>Today</span></div>{chat}<div id='end'></div></div>"
            "<div class='busy-note' role='status'>Reading your reply…</div>" + composer +
            "<div class='wa-help'>Enter to send · Shift + Enter for a new line</div></section></main>")
    script = """<script>
    const reply=document.querySelector('textarea[name=msg]'), log=document.querySelector('.wa-body');
    if(location.hash==='#end') log.scrollTop=log.scrollHeight;
    window.addEventListener('resize',()=>{if(location.hash==='#end') log.scrollTop=log.scrollHeight;});
    document.querySelectorAll('[data-example]').forEach(button=>button.addEventListener('click',()=>{reply.value=button.dataset.example;reply.focus();}));
    reply.addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();reply.form.requestSubmit();}});
    document.querySelectorAll('.wa form').forEach(form=>form.addEventListener('submit',()=>{
      document.querySelector('.wa').classList.add('busy');
      document.querySelector('.busy-note').textContent=form.classList.contains('wa-in')?'Reading your reply…':'Checking your decision…';
    }));
    </script>"""
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>{'Messenger' if fb else 'WhatsApp'} · {h(name)}</title>"
            f"<style>{CSS}{WA_CSS}body{{background:#edf3ee}}</style></head><body>{body}{script}</body></html>")


# ---------------------------------------------------------------- SMS, email and app push
# Same content and rules as the WhatsApp message, shaped for what each channel does well.

GSM = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZ"
          "ÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà^{}\\[~]|€")


def sms_safe(text):
    """Swap characters that would push an SMS out of the GSM alphabet (70 instead of 160 characters per part)."""
    for a, b in (("–", "-"), ("•", "-"), ("’", "'"), ("…", "...")):
        text = text.replace(a, b)
    return text


def sms_parts(text):
    """(characters, number of SMS parts, alphabet). GSM: 160 per SMS, 153 per part when split. Unicode: 70 / 67."""
    if all(c in GSM for c in text):
        n = len(text) + sum(c in "^{}\\[~]|€" for c in text)
        return n, 1 if n <= 160 else -(-n // 153), "GSM"
    n = len(text.encode("utf-16-le")) // 2
    return n, 1 if n <= 70 else -(-n // 67), "Unicode"


SHORT = {}   # path -> short code, so the SMS link stays short
CODES = {}   # short code -> path


def short_link(path):
    if path not in SHORT:
        SHORT[path] = str(len(SHORT) + 1)
        CODES[SHORT[path]] = path
    return f"/c/{SHORT[path]}"


def sms_text(pid, title, items, link):
    """Plain SMS, laid out with line breaks so it reads easily: greeting, the days one per line, how to answer,
    the link. A line break is one character in the GSM alphabet, so it costs almost nothing."""
    ex = f"{items[0]['day'].day} {items[0]['day']:%b} 10-14"
    days = lambda lst, key: compact([(s["day"], s[key]) for s in lst], sep="\n")
    if items[0].get('live_check'):
        return sms_safe(f"Fonecta Connect: {intro(pid, title, items)}\n\nListed hours:\n{days(items, 'shown')}\n\n"
                        f"Are these hours right?\nReply YES or NO, or send your hours.\n\nDetails: {link}")
    known, new = split_known(items)
    blocks = []
    if known:
        blocks.append("Last year you were open:\n" + days(known, "last_hours"))
    if new:
        blocks.append(("For the other days we suggest:\n" if known else "We suggest:\n") + days(new, "hours"))
    return sms_safe(f"Fonecta Connect: Hi {items[0]['name']}!\n{intro(pid, title, items)}\n\n" + "\n\n".join(blocks)
                    + f"\n\n{question_for(pid, title, items)}\nReply YES or NO.\nOr send your own hours, e.g.\n{ex}"
                    f"\n\nAll days: {link}\nWe never ask for passwords.")


CH_CSS = """
.ph{max-width:420px;margin:0 auto;min-height:100vh;font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.sms{background:#fff;color:#000}
.sms-top{text-align:center;padding:14px 10px 10px;border-bottom:1px solid #e5e5ea;background:#f9f9f9}
.sms-top .av{width:44px;height:44px;border-radius:50%;background:#14198a;color:#fff;font-weight:700;display:flex;align-items:center;justify-content:center;margin:0 auto 4px;font-size:15px}
.sms-top b{font-size:13px}
.sms-body{padding:10px 12px 24px}
.sms-when{text-align:center;color:#8e8e93;font-size:12px;margin:6px 0 10px}
.sb{background:#e9e9eb;color:#000;border-radius:18px;padding:8px 13px;margin:4px 0;max-width:80%;white-space:pre-line;word-wrap:break-word}
.sb.me{background:#34c759;color:#fff;margin-left:auto}
.sb a{color:inherit}
.meta{font-size:11px;color:#8e8e93;margin:2px 0 10px 6px}
.sms-in{display:flex;gap:8px;margin-top:14px}
.sms-in input{flex:1;border:1px solid #d1d1d6;border-radius:20px;padding:9px 14px;font:inherit;background:#fff;color:#000}
.sms-in button{width:38px;height:38px;border-radius:50%;border:0;background:#34c759;color:#fff;font-size:16px;cursor:pointer}
.sms-help{font-size:12px;color:#8e8e93;margin-top:6px;text-align:center}
.mail{background:#fff;color:#1b1b22}
.inbox{border-bottom:8px solid #f0f0f3;padding:12px 16px}
.inbox .lbl{font-size:12px;color:#5d5f6b;text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px}
.ir{display:grid;grid-template-columns:1fr auto;gap:0 8px}.ir b{font-size:15px}.ir .tm{font-size:12px;color:#5d5f6b}
.ir .sj{grid-column:1/3;font-weight:600;font-size:14px}.ir .pv{grid-column:1/3;color:#5d5f6b;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mh{display:flex;gap:10px;align-items:center;padding:14px 16px 4px}
.mh .av{width:36px;height:36px;border-radius:50%;background:#14198a;color:#fff;font-weight:700;display:flex;align-items:center;justify-content:center;font-size:13px}
.mh small{display:block;color:#5d5f6b;font-size:12px}
.msub{font-size:19px;margin:8px 16px 12px}
.mb{margin:0 16px 20px;border:1px solid #e3e4ea;border-radius:12px;overflow:hidden}
.mb .bar{background:#14198a;color:#fff;font-weight:700;padding:10px 16px;font-size:14px}
.mb .in{padding:16px}
.mb h3{font-size:14px;margin:14px 0 6px;color:#5d5f6b;font-weight:600}
.dl{width:100%;border-collapse:collapse;font-size:15px}.dl td{padding:7px 0;border-bottom:1px solid #eee}.dl td:last-child{text-align:right;font-weight:600}
.mb .q{font-weight:700;font-size:16px;margin:16px 0 4px}
.mb .btn{color:#14198a;border-color:#14198a}.mb .btn.primary{background:#14198a;color:#fff}.mb .btn.plain{border-color:#d0d1d9;color:#5d5f6b}
.mf{font-size:12px;color:#5d5f6b;padding:0 16px 24px}.mf a{color:#5d5f6b}
.answered{background:#e6f4ec;color:#1d7a4a;border-radius:10px;padding:10px 12px;font-weight:600;margin-bottom:12px}
.lock{min-height:100vh;background:linear-gradient(160deg,#2a2f8f,#5a3d8a 55%,#b0577a);color:#fff;padding:40px 12px 24px}
.clock{text-align:center;font-size:64px;font-weight:300;line-height:1}.ldate{text-align:center;font-size:16px;margin:6px 0 30px;opacity:.9}
.nt{background:rgba(245,245,247,.88);color:#000;border-radius:18px;margin-bottom:8px;overflow:hidden;backdrop-filter:blur(20px)}
.nt-main{display:block;padding:11px 13px;color:inherit;text-decoration:none}
.nt-head{display:flex;align-items:center;gap:7px;font-size:12px;color:#3c3c43;margin-bottom:3px}
.nt-ic{width:20px;height:20px;border-radius:5px;background:#14198a;color:#fff;font-size:9px;font-weight:700;display:flex;align-items:center;justify-content:center}
.nt-head span:last-child{margin-left:auto}
.nt b{display:block;font-size:15px}.nt p{margin:1px 0 0;font-size:15px;white-space:pre-line}
.nt-acts{display:flex;flex-direction:column;border-top:1px solid rgba(60,60,67,.2)}
.nt-acts button,.nt-acts a{background:none;border:0;border-top:1px solid rgba(60,60,67,.12);padding:12px;font:inherit;font-size:16px;color:#007aff;cursor:pointer;text-align:center;text-decoration:none}
.nt-acts>:first-child{border-top:0}
.hint{text-align:center;font-size:13px;opacity:.85;margin-top:14px}
.back{text-align:center;font-size:14px;padding:12px}.lock .back a{color:#fff}
"""


def ch_page(title, body, bg="#d1d7db"):
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>{h(title)}</title>"
            f"<style>{CSS}{CH_CSS}body{{background:{bg}}}</style></head><body>{body}</body></html>")


def back_links(ch, eid, pid):
    return (f"<p class='back muted'><a href='/'>← Connect demo panel</a> · "
            f"<a href='/{ch}/{eid}/{pid}/reset'>Reset</a></p>")


def sms_page(eid, pid, title, items, host):
    """SMS: works on every phone, but no buttons. So: short, plain characters (fewer parts to pay for),
    reply with one word (YES / NO) or the hours, and a short link to the full list."""
    seen_for(eid, pid, items)
    link = host + short_link(f"/b/{eid}/{pid}?via=SMS")
    text = sms_text(pid, title, items, link)
    n, parts, alphabet = sms_parts(text)
    first = h(text).replace(h(link), f"<a href='http://{h(link)}'>{h(link)}</a>")
    chat = (f"<div class='sb'>{first}</div>"
            f"<div class='meta'>Demo note: {n} characters, {parts} SMS part{'s' if parts > 1 else ''} ({alphabet} alphabet)</div>")
    for who, msg in WA_CHAT.get(("s", eid, pid), []):
        chat += f"<div class='sb{' me' if who == 'me' else ''}'>{h(msg)}</div>"
    body = (f"<div class='ph sms'><div class='sms-top'><div class='av'>FC</div><b>Fonecta Connect</b></div>"
            f"<div class='sms-body'><div class='sms-when'>Text Message · Today 16:05</div>{chat}<div id='end'></div>"
            f"<form method='post' action='/s/{eid}/{pid}/reply' class='sms-in'>"
            f"<input name='msg' placeholder='Text Message' autocomplete='off' required aria-label='Reply'>"
            f"<button aria-label='Send'>↑</button></form>"
            f"<p class='sms-help'>Try: YES · NO · {items[0]['day'].day} {items[0]['day']:%b} 10-14 · UNDO · STOP</p></div>"
            f"{back_links('s', eid, pid)}</div>")
    return ch_page(f"SMS · {items[0]['name']}", body, "#e5e5ea")


def email_subject(pid, title, items):
    known, new = split_known(items)
    what = (f"{title} hours" if pid in ("christmas", "new-year")
            else f"Your hours on {items[0]['weekday'][:3]} {items[0]['dm']}")
    return f"{what}: same as last year?" if known and not new else f"{what}: please check"


def email_page(eid, pid, title, items):
    """Email: 60% never replied in the pilot, so the subject and preview line do the work: they say what it's
    about and what we suggest before the owner even opens it. Inside: the days as a clear list and big buttons.
    No login needed."""
    seen_for(eid, pid, items)
    name, n = items[0]["name"], len(items)
    known, new = split_known(items)

    def rows(lst, key):
        return "".join(f"<tr><td>{h(s['weekday'][:3])} {h(s['dm'])} · {h(s['day_name'])}</td>"
                       f"<td>{h(state(s[key], 'en'))}</td></tr>" for s in lst)
    lists = ""
    if known:
        lists += f"<h3>Last year you were open:</h3><table class='dl'>{rows(known, 'last_hours')}</table>"
        if new:
            lists += "<p class='muted' style='margin:8px 0 0'>We'd use the same hours this year.</p>"
    if new:
        lists += (f"<h3>{'For the other days we suggest:' if known else 'We suggest:'}</h3>"
                  f"<table class='dl'>{rows(new, 'hours')}</table>")
    ans = WA.get(("e", eid, pid))
    done = {"yes": "You answered: Yes, update all.", "no": "You answered: No, keep as it is.",
            "stop": "You stopped holiday suggestions.", "other": "You chose your own hours."}.get(ans, "")
    yes = f"Yes, update all {n} days" if n > 1 else "Yes, update"
    no = 'No, keep as it is'
    stop_label = 'Stop holiday suggestions'
    if items[0].get('live_check'):
        lists = f"<h3>Your listed hours:</h3><table class='dl'>{rows(items, 'shown')}</table>"
        yes, no, stop_label = 'Yes, confirm hours', 'Not relevant', 'Stop event checks'
        done = {'yes': 'You confirmed your hours.', 'no': 'You marked this check irrelevant.',
                'stop': 'You stopped event checks.', 'other': 'You chose your own hours.'}.get(ans, '')
    buttons = (f"<a class='btn primary' href='/e/{eid}/{pid}/yes'>{yes}</a>"
               f"<a class='btn' href='/b/{eid}/{pid}?via=EMAIL'>Other hours</a>"
               f"<a class='btn plain' href='/e/{eid}/{pid}/no'>{no}</a>")
    subject = email_subject(pid, title, items)
    body = (f"<div class='ph mail'>"
            f"<div class='inbox'><div class='lbl'>Inbox</div><div class='ir'><b>Fonecta Connect</b><span class='tm'>16:05</span>"
            f"<span class='sj'>{h(subject)}</span><span class='pv'>{h(summary(items))}</span></div></div>"
            f"<div class='mh'><div class='av'>FC</div><div><b>Fonecta Connect</b><small>to {h(name)} · 16:05</small></div></div>"
            f"<h1 class='msub'>{h(subject)}</h1>"
            f"<div class='mb'><div class='bar'>Fonecta Connect</div><div class='in'>"
            f"{'<div class=answered>' + h(done) + '</div>' if done else ''}"
            f"<p><b>Hi {h(name)}!</b></p>"
            f"<p>{h(intro(pid, title, items))} {h(proposal_context(items))}</p>{lists}"
            f"<p class='q'>{h(question_for(pid, title, items))}</p>"
            f"<p class='muted' style='margin:0'>One tap, no login needed.</p>{'' if ans else buttons}</div></div>"
            f"<p class='mf'>We never ask for logins or passwords. You get this email because {h(name)} uses "
            f"Fonecta Connect. <a href='/e/{eid}/{pid}/stop'>{stop_label}</a></p>"
            f"{back_links('e', eid, pid)}</div>")
    return ch_page(f"Email · {name}", body, "#f0f0f3")


def email_confirm(eid, pid, title, items, act):
    """A link in an email only opens this page; the answer is saved by the button here. Email security scanners
    open every link in a message, so a link alone must never change anyone's hours."""
    name, n = items[0]["name"], len(items)
    live = bool(items[0].get('live_check'))
    if ("e", eid, pid) in WA:
        inner = "<h2>You've already answered this message.</h2><p class='muted'>To change a day, use the link below.</p>" \
                f"<a class='btn' href='/b/{eid}/{pid}?via=EMAIL'>Change a day</a>"
    elif act == "yes":
        days = "".join(f"<li>{s['weekday'][:3]} {h(s['dm'])}: <b>{h(state(s['hours'], 'en'))}</b></li>" for s in items)
        inner = (f"<h2>{'Confirm your listed hours?' if live else 'Update your ' + h(title) + ' hours?'}</h2><ul class='res'>{days}</ul>"
                 f"<form method='post' action='/e/{eid}/{pid}'><button class='btn primary' name='a' value='yes'>"
                 f"{'Yes, confirm hours' if live else 'Yes, update all ' + str(n) + ' days' if n > 1 else 'Yes, update'}</button></form>"
                 f"<a class='btn plain' href='/b/{eid}/{pid}?via=EMAIL'>Change a day instead</a>")
    elif act == "no":
        inner = (f"<h2>Keep your hours as they are?</h2><p class='muted'>Your existing hours stay unchanged.</p>"
                 f"<form method='post' action='/e/{eid}/{pid}'><button class='btn primary' name='a' value='no'>"
                 f"No, keep as it is</button></form><a class='btn plain' href='/e/{eid}/{pid}'>Back</a>")
    else:
        subject = 'event checks' if live else 'holiday suggestions'
        inner = (f"<h2>Stop {subject}?</h2><p class='muted'>We won't send {h(name)} {'event checks' if live else 'suggestions for holidays'} "
                 f"any more. Your hours stay as they are.</p>"
                 f"<form method='post' action='/e/{eid}/{pid}'><button class='btn primary' name='a' value='stop'>"
                 f"Stop suggestions</button></form><a class='btn plain' href='/e/{eid}/{pid}'>Back</a>")
    body = (f"<div class='brand'><span class='dot'></span>Fonecta Connect</div><div class='card'>{inner}"
            f"<p class='muted' style='margin:14px 0 0'>{UI['en']['safe']}</p></div>"
            f"<p class='muted' style='text-align:center;margin-top:14px'><a href='/'>← Connect demo panel</a></p>")
    return page(name, body)


def push_text(pid, title, items):
    """(title, body). Lock screens show about 40 characters of title and 4 lines of body, so keep it short;
    the full list is one tap away in the app."""
    known, new = split_known(items)
    if known and not new:
        head = f"{title}: same hours as last year?"
    elif pid in ("christmas", "new-year"):
        head = f"Check your {title} hours"
    else:
        head = f"{title}: check your hours"
    if len(head) > 44:
        head = head[:43].rstrip() + "…"
    body = summary(items)
    if not (known and not new):
        body += " " + question_for(pid, title, items)
    if len(body) > 150:
        body = body[:149].rsplit(" ", 1)[0].rstrip(",.") + "…"
    return head, body


def push_page(eid, pid, title, items):
    """App push: free to send and the app can show every day, but it's easy to swipe away (68% no reply in the
    pilot). So: the answer is in the notification title, and the owner can answer from the lock screen with
    the notification's own buttons, without opening the app."""
    seen_for(eid, pid, items)
    k, n = ("p", eid, pid), len(items)
    head, text = push_text(pid, title, items)
    app = f"/b/{eid}/{pid}?via=APP_PUSH"

    def note(t, b, href, when="now"):
        return (f"<a class='nt-main' href='{href}'><div class='nt-head'><span class='nt-ic'>FC</span>"
                f"<span>FONECTA CONNECT</span><span>{when}</span></div>{'<b>' + h(t) + '</b>' if t else ''}"
                f"<p>{h(b)}</p></a>")
    if k not in WA:
        yes = "Yes, update all" if n > 1 else "Yes, update"
        no = 'No, keep as it is'
        if items[0].get('live_check'):
            yes, no = 'Yes, confirm hours', 'Not relevant'
        notes = (f"<div class='nt'>{note(head, text, app)}"
                 f"<form method='post' action='/p/{eid}/{pid}' class='nt-acts'>"
                 f"<button name='a' value='yes'>{yes}</button><a href='{app}'>Other hours</a>"
                 f"<button name='a' value='no'>{no}</button></form></div>"
                 f"<p class='hint'>Tap the notification to open the app with every day listed, or answer with the "
                 f"buttons (long-press on iPhone, swipe down on Android).</p>")
    else:
        # Newest notification first. You can't reply to a notification, so undo is a button on it.
        msgs = [msg for who, msg in WA_CHAT.get(k, []) if who == "them"][::-1]
        notes = ""
        for i, msg in enumerate(msgs):
            undo = i == 0 and "UNDO" in msg
            notes += (f"<div class='nt'>{note('', msg.replace('Reply UNDO to undo.', '').strip(), app)}"
                      + (f"<form method='post' action='/p/{eid}/{pid}' class='nt-acts'><button name='a' value='undo'>"
                         f"Undo</button></form>" if undo else "") + "</div>")
        notes += f"<div class='nt'>{note(head, text, app, '16:05')}</div>"
    body = (f"<div class='ph lock'><div class='clock'>16:05</div><div class='ldate'>Lock screen</div>{notes}"
            f"{back_links('p', eid, pid)}</div>")
    return ch_page(f"App push · {items[0]['name']}", body, "#1b1b2f")


def declined_page(eid, title):
    return page(BL.ents[eid]["name"],
                "<div class='brand'><span class='dot'></span>Fonecta Connect</div><div class='card'>"
                f"<h2>{UI['en']['declined']}</h2><p class='muted'>Google keeps showing your regular "
                f"hours for {h(title)}.</p></div><p class='muted' style='text-align:center;margin-top:14px'>"
                "<a href='/'>← Connect demo panel</a></p>")


def bundle_done(eid, title, results, always, res=None):
    res = res or {}
    by_date = {x["date"]: x for x in res.get("items", [])}
    verified = {dt for dt, x in by_date.items() if x["status"] in ("APPLIED", "ALREADY_SET")}
    lines = []
    for s, hrs in results:
        dt = s["day"].isoformat()
        item = by_date.get(dt, {})
        label = state(item.get("hours", hrs), 'en') if dt in verified else "left as it is" if hrs is None else "not saved"
        lines.append(f"<li>{h(s['dm'])}: <b>{h(label)}</b></li>")
    message = res.get("message") or "No hours were changed."
    scope = "Real Yext sandbox; public listings are unverified." if S5.applier.primary.name == "sandbox" else ""
    heading = f"Done! {title} hours saved." if verified else "No saved changes confirmed"
    body = (f"<div class='brand'><span class='dot'></span>Fonecta Connect</div><div class='card'>"
            f"<h2>{h(heading)}</h2><ul class='res'>{''.join(lines)}</ul><p style='white-space:pre-line'>{h(message)}</p><p class='muted'>{h(scope)}</p>"
            f"{'<p class=muted style=margin-top:14px>' + UI['en']['always_on'] + '</p>' if always and verified else ''}</div>"
            f"<p class='muted' style='text-align:center;margin-top:14px'><a href='/'>← Connect demo panel</a></p>")
    return page(BL.ents[eid]["name"], body)


def no_message(c):
    why = {"FLAG": "Signals say this business may have closed for good. A Fonecta person must review it; no review ticket has been created.",
           "NO_ACTION": "The supplied listing snapshot already has suitable hours, or Connect has no reliable suggestion yet."}[c["action"]]
    body = (f"<div class='brand'><span class='dot'></span>Connect · demo control panel</div><div class='card'>"
            f"<h2>No message for {h(c['name'])}</h2><p class='muted'>{h(c['day_name'])} {h(c['dm'])}. {why}</p>"
            f"<p class='muted'>Google shows: {h(state(c['shown'], c['lang']))}</p></div>"
            f"<p class='muted' style='text-align:center;margin-top:14px'><a href='/'>← Connect demo panel</a></p>")
    return page(c["name"], body)


def owner_message(c, err=""):
    if c["action"] != "SUGGEST":
        return no_message(c)
    u = UI[c["lang"]]
    sug = state(c["hours"], c["lang"])
    body = (f"<div class='brand'><span class='dot'></span>{u['from_c']}</div><div class='card'>"
            f"<h1>{h(TXT[c['lang']]['hi'].format(name=c['name']))}</h1>"
            f"<p>{h(c['intro'])} {h(c['reason'])}</p>"
            f"<div class='cmp'><div class='old'><small>{u['now']}</small><b>{h(state(c['shown'], c['lang']))}</b></div>"
            f"<div class='new'><small>{u['sug']} {h(c['dm'])}</small><b>{h(sug)}</b></div></div>"
            f"<form method='post' action='/m/{c['eid']}/{c['day']}/approve'>"
            f"<input type='hidden' name='hours' value='{h(c['hours'])}'>"
            f"<label class='opt'><input type='checkbox' name='always' value='1'>{u['always']}</label>"
            f"<button class='btn primary' type='submit'>{u['approve']}</button></form>"
            f"<a class='btn' href='/m/{c['eid']}/{c['day']}/edit'>{u['edit']}</a>"
            f"<form method='post' action='/m/{c['eid']}/{c['day']}/decline'>"
            f"<button class='btn plain' type='submit'>{u['no']}</button></form>"
            f"{'<p class=err>' + h(err) + '</p>' if err else ''}"
            f"<p class='muted' style='margin:14px 0 0'>{u['safe']}</p></div>")
    return page(c["name"], body)


def owner_edit(c, err=""):
    u = UI[c["lang"]]
    first = c["hours"].split(",")[0] if c["hours"] not in ("", "closed") else c["shown"].split(",")[0]
    o, cl = (first.split("-") + ["", ""])[:2] if first != "closed" else ("10:00", "16:00")
    closed = c["hours"] == "closed"
    body = (f"<div class='brand'><span class='dot'></span>{u['from_c']}</div><div class='card'>"
            f"<h2>{h(c['day_name'])} {h(c['dm'])}</h2>"
            f"<form method='post' action='/m/{c['eid']}/{c['day']}/edit'>"
            f"<label class='opt'><input type='radio' name='mode' value='open' {'' if closed else 'checked'}>{u['open_l']}</label>"
            f"<div class='row2'><div><label>{u['from']}</label><input type='time' name='open' value='{h(o)}'></div>"
            f"<div><label>{u['to']}</label><input type='time' name='close' value='{h(cl)}'></div></div>"
            f"<label class='opt'><input type='radio' name='mode' value='closed' {'checked' if closed else ''}>{u['closed_l']}</label>"
            f"<label class='opt'><input type='checkbox' name='always' value='1'>{u['always']}</label>"
            f"{'<p class=err>' + h(err) + '</p>' if err else ''}"
            f"<button class='btn primary' type='submit'>{u['save']}</button></form>"
            f"<a class='btn plain' href='/m/{c['eid']}/{c['day']}'>{u['back']}</a></div>")
    return page(c["name"], body)


def owner_done(c, hours, always, res):
    return bundle_done(c["eid"], c["day_name"], [(c, hours)], always, res)


def owner_declined(c):
    u = UI[c["lang"]]
    body = (f"<div class='brand'><span class='dot'></span>{u['from_c']}</div><div class='card'>"
            f"<h2>{u['declined']}</h2><p class='muted'>{u['declined2']}</p></div>"
            f"<p class='muted' style='text-align:center;margin-top:14px'><a href='/'>← Connect demo panel</a></p>")
    return page(c["name"], body)


# ---------------------------------------------------------------- local setup: save the AI token

def setup_page(msg=""):
    """A local-only form for pasting the OpenShift AI token into .env. The token is never shown."""
    status = "a token is saved" if os.environ.get("OPENSHIFT_AI_TOKEN") else "no token saved yet"
    body = (f"<div class='brand'><span class='dot'></span>Setup · AI model token</div><div class='card'>"
            f"<h2>Paste the OpenShift AI token</h2>"
            f"<p class='muted'>It's saved only in the .env file on this computer (never uploaded) and isn't shown again. "
            f"Now: {status}.</p>{msg}"
            f"<form method='post' action='/setup'><input type='password' name='token' autocomplete='off' required "
            f"aria-label='Token' style='width:100%;font-size:16px;padding:12px;border-radius:10px;border:1px solid var(--line)'>"
            f"<button class='btn primary' type='submit'>Save token</button></form></div>")
    return page("Setup", body)


def save_token(token):
    token = token.strip()
    if len(token) < 100:   # a real service-account token is a long JWT; short text is usually the wrong clipboard
        return setup_page(f"<p class='err'>That doesn't look like the token ({len(token)} characters; a real one is "
                          f"hundreds long). Copy it again and paste.</p>")
    env = os.path.join(os.path.dirname(HERE), ".env")
    lines = open(env, encoding="utf-8").read().splitlines() if os.path.exists(env) else []
    lines = [l for l in lines if not l.startswith("OPENSHIFT_AI_TOKEN=")] + [f"OPENSHIFT_AI_TOKEN={token}"]
    with open(env, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.environ["OPENSHIFT_AI_TOKEN"] = token
    return setup_page(f"<div class='ok'>Saved ({len(token)} characters). You can close this page.</div>")


# ---------------------------------------------------------------- post suggestions on WhatsApp (posts.py)
TODAY = BL.snap                     # the challenge's "today" (6 Nov 2026)


def week_hours(eid):
    """'Mon–Fri 7:30–17\nSat 9–15\nSun closed' from the listing's regular hours next week."""
    from datetime import timedelta
    start = TODAY + timedelta(days=7 - TODAY.weekday())
    days = [(start + timedelta(days=i)) for i in range(7)]
    runs = []
    for d in days:
        hrs = BL.regular(eid, d)
        if runs and runs[-1][2] == hrs:
            runs[-1][1] = d
        else:
            runs.append([d, d, hrs])
    return "\n".join(f"{a:%a}{'–' + format(b, '%a') if b != a else ''} {'closed' if hrs == 'closed' else nice(hrs)}"
                     for a, b, hrs in runs)


def post_ok(eid):
    """No posts for owners who opted out, or businesses that may have closed for good (a person checks those)."""
    return eid in TARGETS and not opted_out(eid) and suggestion(eid, TARGETS[eid][0])["action"] != "FLAG"


def monthly_post(eid):
    ent = BL.ents[eid]
    head, text = posts.monthly_text(ent["name"], ent["address"]["city"], week_hours(eid))
    return posts.make(f"{eid}~monthly", eid, "monthly", head, text, ent.get("c_segment", ""),
                      f"Hi {ent['name']}! Here's your monthly post for {posts.PUBLISH_TO}. It goes live on "
                      f"{posts.day_label(TODAY + __import__('datetime').timedelta(days=posts.MONTHLY_GO_LIVE_DAYS))} "
                      f"unless you skip it:", TODAY)


def event_options(eid):
    """Every event near the business in the next 5 weeks, with whether it's relevant and why:
    [(day, event, metres, relevant, reason)], best relevant first."""
    from datetime import timedelta
    ent = BL.ents[eid]
    out = []
    for ev in EVENTS.values():
        st = ev.get("start_time") or ""
        try:
            day = date.fromisoformat(st[:10])
        except ValueError:
            continue
        if not (TODAY < day <= TODAY + timedelta(days=posts.EVENT_DAYS)):
            continue
        metres = posts.metres_between(ent.get("geocodedCoordinate"),
                                      ev.get("location", {}).get("position", {}).get("coordinates"))
        if metres is None or metres > 2 * posts.MAX_METRES:
            continue
        end = (ev.get("end_time") or "")[11:16] or st[11:16]
        ok, why = posts.event_relevance(ent.get("c_segment", ""), ev.get("keywords", []), metres,
                                        ev.get("x_expected_attendance"), BL.displayed(eid, day), st[11:16], end)
        out.append((day, ev, metres, ok, why))
    # Most visitors per metre first: a big crowd right next door beats a small one far away.
    # Relevant first, most visitors per metre first; then the rest nearest first (its reason is shown).
    out.sort(key=lambda o: (0, -(o[1].get("x_expected_attendance") or 0) / max(o[2], 100)) if o[3] else (1, o[2]))
    return out


def next_event(eid):
    """The most relevant event near the business, or None. Respects an owner who turned event suggestions off."""
    if not LEARN.reason_on(eid, "event"):
        return None
    best = next((o for o in event_options(eid) if o[3]), None)
    return (best[0], best[1], best[2], best[4]) if best else None


def event_post(eid):
    found = next_event(eid)
    if not found:
        return None
    day, ev, metres, why = found
    ent = BL.ents[eid]
    name, venue = ev["name"]["en"], ev.get("location", {}).get("name", {}).get("en", "")
    start = (ev.get("start_time") or "")[11:16]
    when = f"{posts.day_label(day)}{' at ' + start if start else ''}"
    hrs = BL.displayed(eid, day)
    head, text = posts.event_text(ent["name"], name, venue, when, ev.get("x_expected_attendance"), metres,
                                  f"open {nice(hrs)}")
    p = posts.make(f"{eid}~event~{day}", eid, "event", head, text, ent.get("c_segment", ""),
                   f"Hi {ent['name']}! {name} is coming up near you on {posts.day_label(day)}. "
                   f"Want to post about it on {posts.PUBLISH_TO}? Here's a draft:", TODAY)
    p["why"] = f"{metres:,} m away, {why}"
    return p


def campaign_post(eid):
    """The owner announced an offer on their own Instagram: suggest posting it on Google and Facebook too.
    The post is written from the facts only (kind of offer, discount, dates), never copied from the owner's post."""
    found = social_hours.campaigns(social_hours.SAMPLES.get(eid, []), TODAY)
    if not found:
        return None
    c, ent = found[0], BL.ents[eid]
    head, text = posts.campaign_text(ent["name"], c["facts"])
    p = posts.make(f"{eid}~campaign~{c['posted']}", eid, "campaign", head, text, ent.get("c_segment", ""),
                   f"Hi {ent['name']}! We saw your offer on Instagram "
                   f"({posts.day_label(date.fromisoformat(c['posted']))}). Shall we also post it on {posts.PUBLISH_TO}, "
                   f"so people who search for you see it? Here's a short post with the details. Tap Edit text to "
                   f"add what's on offer:", TODAY)
    p["why"] = "facts from the owner's Instagram post (not copied)" + (
        f", runs until {posts.day_label(c['until'])}" if c["until"] else "")
    return p


def seasonal_post(eid):
    """Christmas idea for this kind of business (menu, treats, flowers…): a question first."""
    ent = BL.ents[eid]
    idea = posts.seasonal_idea(ent.get("c_segment", ""), TODAY)
    if not idea:
        return None
    key, head, question, example, template = idea
    phone = re.sub(r"^\+(358|46|47)", r"+\1 ", ent.get("mainPhone") or "") or "call us"
    return posts.make_seasonal(f"{eid}~season~{key}", eid, head,
                               f"Hi {ent['name']}! Christmas is coming. {question} Tell us in a message and we'll "
                               f"turn it into a post for {posts.PUBLISH_TO}.", example, template, {"phone": phone},
                               ent.get("c_segment", ""), TODAY)


def hours_post(eid, pid, title, confirmed):
    """After the owner confirms holiday hours: a post built only from those hours. confirmed = [(item, hours)]."""
    if not confirmed or not post_ok(eid):
        return None
    days = [(s["day"], s["day_name"], "closed" if hrs == "closed" else f"open {nice(hrs)}") for s, hrs in confirmed]
    head, text = posts.holiday_text(BL.ents[eid]["name"], title, days)
    what = f"your {title} hours" if pid in ("christmas", "new-year") else "these hours"
    return posts.make(f"{eid}~{pid}~hours", eid, "holiday", head, text, BL.ents[eid].get("c_segment", ""),
                      f"One more idea: shall we also post {what} on {posts.PUBLISH_TO}, so customers see them? "
                      f"Here's a draft:", TODAY, title=title)


def find_post(post_id):
    if post_id in posts.POSTS:
        return posts.POSTS[post_id]
    eid = post_id.split("~")[0]
    if eid not in BL.ents or not post_ok(eid):
        return None
    if post_id == f"{eid}~monthly":
        return monthly_post(eid)
    if "~campaign~" in post_id:
        cp = campaign_post(eid)
        return cp if cp and cp["id"] == post_id else None
    if "~season~" in post_id:
        sp = seasonal_post(eid)
        return sp if sp and sp["id"] == post_id else None
    ev = event_post(eid) if "~event~" in post_id else None
    return ev if ev and ev["id"] == post_id else None


def post_page(post):
    """A post suggestion as its own WhatsApp chat (monthly and event posts)."""
    name = BL.ents[post["eid"]]["name"]
    pid = urllib.parse.quote(post["id"])
    chat = posts.chat_html(post, name, f"/wp/{pid}")
    if post["status"] in ("details", "suggested", "editing", "published"):   # details: the owner types their idea
        chat += (f"<form method='post' action='/wp/{pid}/reply' class='wa-in'><input name='msg' placeholder='Message' "
                 f"autocomplete='off' required aria-label='Reply'><button aria-label='Send'>➤</button></form>")
    top = ("<div class='wa-top'><div class='wa-av'>FC</div><div><b>Fonecta Connect</b>"
           "<small>Business account</small></div></div>")
    body = (f"<div class='wa'>{top}<div class='wa-body'><div class='wa-day'><span>{posts.day_label(post['sent'])}"
            f"</span></div>{chat}<div id='end'></div></div></div><p class='muted' style='text-align:center'>"
            f"<a href='/'>← Connect demo panel</a> · <a href='/wp/{pid}/reset'>Reset this chat</a></p>")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>Post · {h(name)}</title>"
            f"<style>{CSS}{WA_CSS}body{{background:#d1d7db}}</style></head><body>{body}</body></html>")


# The panel shows one example per post type (the best one), not every business: the same rules run for all.
POST_SHOWCASE = {"FI-D0001": "monthly", "FI-D0010": "event", "FI-D0002": "seasonal", "FI-D0004": "campaign"}


def posts_card():
    """'Post suggestions on WhatsApp' card on the Connect panel: the best example of each post type."""
    rows = []
    for eid in DEMO:
        if not post_ok(eid):
            continue
        kind = POST_SHOWCASE.get(eid)
        cells = []
        if kind == "monthly":
            m = monthly_post(eid)
            mid = urllib.parse.quote(m["id"])
            cells.append(f"<a href='/wp/{mid}'>Monthly post</a> <span class='tag'>{h(posts.status_text(m))}</span>"
                         + (f" <form method='post' action='/wp/{mid}/days' style='display:inline'>"
                            f"<button class='tag' style='cursor:pointer' name='n' value='6'>Simulate: 6 days, no reply</button>"
                            f"<button class='tag' style='cursor:pointer' name='n' value='1'>+1 day</button></form>"
                            if m["status"] == "suggested" else ""))
        ev = event_post(eid) if kind == "event" else None
        if ev:
            opts = event_options(eid)
            cells.append(f"<a href='/wp/{urllib.parse.quote(ev['id'])}'>Event post: {h(ev['headline'])}</a> "
                         f"<span class='tag'>{h(posts.status_text(ev))}</span><br><span class='muted' "
                         f"style='font-size:13px'>Relevant: {h(ev['why'])}. Checked {len(opts)} events nearby, "
                         f"{sum(1 for o in opts if o[3])} relevant.</span>")
        cp = campaign_post(eid) if kind == "campaign" else None
        if cp:
            cells.append(f"<a href='/wp/{urllib.parse.quote(cp['id'])}'>Campaign post: {h(cp['headline'])}</a> "
                         f"<span class='tag'>{h(posts.status_text(cp))}</span><br><span class='muted' "
                         f"style='font-size:13px'>{h(cp['why'])}</span>")
        sp = seasonal_post(eid) if kind == "seasonal" else None
        if sp:
            cells.append(f"<a href='/wp/{urllib.parse.quote(sp['id'])}'>Christmas idea: {h(sp['headline'])}</a> "
                         f"<span class='tag'>{h(posts.status_text(sp))}</span>")
        for p in posts.POSTS.values():      # holiday posts appear after a yes in any hours chat
            if p["eid"] == eid and p["kind"] == "holiday":
                cells.append(f"Holiday post: {h(p['headline'])} <span class='tag'>{h(posts.status_text(p))}</span>")
        if not cells:
            continue
        rows.append(f"<tr><td><b>{h(BL.ents[eid]['name'])}</b><br><span class='muted'>{h(eid)}</span></td>"
                    f"<td>{'<br>'.join(cells)}</td></tr>")
    return (f"<div class='card' style='margin-top:14px'><h1>Post suggestions on WhatsApp</h1>"
            f"<p class='muted'>Fonecta's monthly post today comes by email and needs a login to change. Here it's a "
            f"WhatsApp preview with picture, text and buttons: <b>monthly post</b> goes live after 7 days unless the "
            f"owner skips it (reminder the day before); <b>event</b>, <b>Christmas idea</b> and <b>holiday hours</b> "
            f"posts need a yes. Event posts only for events that fit the business: a café, restaurant or bar near any "
            f"big event, a shop only near Christmas markets and festivals, within 1 km, 2,000+ visitors, and open while "
            f"visitors are around. Christmas ideas ask first (\u201cDo you have a Christmas menu?\u201d) and turn the "
            f"owner's own answer into the post. <b>Campaign</b> posts: when the owner announces an offer on their "
            f"own Instagram, Connect suggests posting it on Google and Facebook too, written from the facts only (offer, discount, dates), never copying their post (needs a yes). Holiday "
            f"posts are offered in the hours chat right after the owner confirms the hours. Publishing to Google and "
            f"Facebook is simulated (the Yext mock has no posts). Pictures: only the business's own photos from Connect, never event photos (the demo shows a stand-in card). Monthly text is a demo sample from the business's own "
            f"listing; in Fonecta's service it comes from their website.</p>"
            f"<p class='muted'>Shown: the best example of each post type. The same rules run for every business.</p>"
            f"<table><tr><th>Business</th><th>Posts</th></tr>{''.join(rows)}</table></div>")


# ---------------------------------------------------------------- hours from the owner's Instagram (social_hours.py)
# Only businesses that connected their professional Instagram to Connect. Demo: SAMPLE captions.
IG = {}   # eid -> {"diffs": [...], "answer": None | "yes" | "no", "chat": [(who, text)]}
IG_REPLACED = {}   # (eid, pid) -> (title, days left out, days left): holiday message days the Instagram message covers


def instagram_days(eid):
    """Every day the owner announced on their own Instagram (whether or not Google already matches)."""
    if S5.applier.primary.name == 'sandbox' or eid not in social_hours.SAMPLES or not post_ok(eid):
        return set()
    ig_state(eid)          # the Instagram message goes out with them
    days = set()
    for p in social_hours.SAMPLES[eid]:
        days |= set(social_hours.read_caption(p["caption"], TODAY))
    return days


def google_now(eid, d):
    """What Google shows for a day right now: special hours in Yext (step 5's snapshot), else regular hours."""
    k = d.isoformat()
    return S5.snapshot(eid, [k]).get(k) or BL.regular(eid, d)


def ig_state(eid):
    """The check is made once, when the message goes out (later changes don't rewrite a sent message)."""
    if eid not in IG:
        IG[eid] = {"diffs": social_hours.check(social_hours.SAMPLES.get(eid, []), lambda d: google_now(eid, d), TODAY),
                   "answer": None, "chat": []}
    return IG[eid]


def ig_day(d, eid=None):
    name = (holiday_name(BL.ents[eid]["meta"]["countryCode"], d) if eid else None) or social_hours.DAY_NAME.get((d.month, d.day))
    return f"{name} ({posts.day_label(d)})" if name else posts.day_label(d)


def ig_hours(hrs):
    return "closed" if hrs == "closed" else f"open {nice(hrs)}"


def ig_message(eid, st):
    """(message, question). The owner's own post is the reason; nothing about other businesses."""
    name, diffs = BL.ents[eid]["name"], st["diffs"]
    others = max(len(where_it_shows(eid)) - 1, 0)
    if len(diffs) == 1:
        x = diffs[0]
        body = (f"We saw on your Instagram that you're {ig_hours(x['instagram'])} on {ig_day(x['day'], eid)}. "
                f"Google still shows you {ig_hours(x['google'])}.")
    else:
        body = "We saw your holiday hours on Instagram, but Google still shows something else:\n" + "\n".join(
            f"• {ig_day(x['day'], eid)}: {ig_hours(x['instagram'])} (Google: {ig_hours(x['google'])})" for x in diffs)
    post = diffs[0]["post"]
    msg = (f"Hi {name}! This is Fonecta Connect.\n\n{body}\n\n"
           f"Your post ({posts.day_label(date.fromisoformat(post['posted']))}): \"{post['caption']}\"")
    return msg, f"Shall we update Google and {others} other places?"


def ig_page(eid):
    st = ig_state(eid)
    name = BL.ents[eid]["name"]
    if not st["diffs"]:
        chat = ("<div class='b'><i>No message: the owner's Instagram doesn't announce special hours that differ from "
                "Google, so Connect stays quiet.</i></div>")
    else:
        msg, q = ig_message(eid, st)
        ans = st["answer"]
        btns = "".join(f"<button class='qb{' on' if ans == k else ''}' name='a' value='{k}' {'disabled' if ans else ''}>"
                       f"<span aria-hidden='true'>↩</span> {h(label)}</button>" for k, label in (("yes", "Yes, update"), ("no", "No")))
        chat = (f"<form method='post' action='/ig/{eid}' class='tpl'><div class='b'>{h(msg)}\n\n<b>{h(q)}</b>"
                f"<span class='ft'>We only read your own posts, because you connected Instagram to Connect. "
                f"We never ask for logins or passwords.</span><span class='t'>10:00</span></div>{btns}</form>")
        for who, text in st["chat"]:
            chat += f"<div class='b{' me' if who == 'me' else ''}'>{h(text)}<span class='t'>10:0{'2 ✓✓' if who == 'me' else '2'}</span></div>"
        if ans:
            chat += ("<form method='post' action='/ig/%s/reply' class='wa-in'><input name='msg' placeholder='Message' "
                     "autocomplete='off' required aria-label='Reply'><button aria-label='Send'>➤</button></form>" % eid)
    top = ("<div class='wa-top'><div class='wa-av'>FC</div><div><b>Fonecta Connect</b>"
           "<small>Business account</small></div></div>")
    body = (f"<div class='wa'>{top}<div class='wa-body'><div class='wa-day'><span>Today 10:00</span></div>{chat}"
            f"<div id='end'></div></div></div><p class='muted' style='text-align:center'><a href='/'>← Connect demo panel</a> · "
            f"<a href='/ig/{eid}/reset'>Reset this chat</a></p>")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>Instagram hours · {h(name)}</title>"
            f"<style>{CSS}{WA_CSS}body{{background:#d1d7db}}</style></head><body>{body}</body></html>")


def ig_answer(eid, a):
    st = ig_state(eid)
    if st["answer"] or a not in ("yes", "no") or not st["diffs"]:
        return
    st["answer"] = a
    st["chat"].append(("me", "Yes, update" if a == "yes" else "No"))
    items = [({"day": x["day"]}, "ACCEPTED" if a == "yes" else "REJECTED", x["instagram"] if a == "yes" else None)
             for x in st["diffs"]]
    res = apply_via_step5(eid, "instagram", "WHATSAPP", items)
    if a == "yes":
        st["chat"].append(("them", without_where(res["message"], eid) or "Your hours are already up to date."))
        notes = problems_text(res)
        if notes:
            st["chat"].append(("them", "\n".join(notes)))
        DECISIONS[(eid, "instagram")] = f"Instagram hours: yes, {len(res['applied'])} applied by step 5"
    else:
        st["chat"].append(("them", "OK, we won't change anything. If your plans change, just reply here."))
        DECISIONS[(eid, "instagram")] = "Instagram hours: declined"


def ig_reply(eid, text):
    st = ig_state(eid)
    text = text.strip()
    if not text or not st["answer"]:
        return
    st["chat"].append(("me", text))
    undo = S5.handle_reply(eid, text, language="en")          # UNDO puts the latest change back
    if undo is not None:
        st["chat"].append(("them", undo["message"] or "There's nothing to undo."))
        DECISIONS[(eid, "instagram")] = "Instagram hours: undone"
    else:
        st["chat"].append(("them", POLITE_REPLY if is_polite(text) else "Thanks! A Fonecta person will read your message."))


def ig_card():
    rows = []
    for eid in DEMO:
        if S5.applier.primary.name == 'sandbox' or eid not in social_hours.SAMPLES or not post_ok(eid):
            continue
        st = ig_state(eid)
        p = social_hours.SAMPLES[eid][-1]
        found = social_hours.read_caption(p["caption"], TODAY)
        got = ", ".join(f"{posts.day_label(d)} {ig_hours(v)}" for d, v in sorted(found.items())) or "no special hours"
        if st["diffs"]:
            what = (f"<a href='/ig/{eid}'>WhatsApp message</a>: {len(st['diffs'])} day(s) differ from Google"
                    + (f" <span class='tag'>{h(DECISIONS[(eid, 'instagram')])}</span>" if (eid, 'instagram') in DECISIONS else ""))
        else:
            what = ("No message: " +
                    ("Google already matches" if found else "no special hours in the post"))
        rows.append(f"<tr><td><b>{h(BL.ents[eid]['name'])}</b><br><span class='muted'>{h(eid)} · Instagram connected"
                    f" (sample)</span></td><td><i>“{h(p['caption'])}”</i><br><span class='muted'>Read as: {h(got)}</span>"
                    f"<br>{what}</td></tr>")
    return (f"<div class='card' style='margin-top:14px'><h1>Hours from the owner's Instagram</h1>"
            f"<p class='muted'>Owners often announce holiday hours on Instagram but never update Google. For owners who "
            f"connected their business Instagram to Connect, Connect reads their <b>own</b> posts (Meta's official API, "
            f"with their permission, no scraping), finds dates and hours in Finnish, Swedish, Norwegian or English, "
            f"compares them with Google, and asks on WhatsApp. Nothing changes without a yes. The challenge data has no "
            f"social media, so these captions are <b>samples</b> (<code>social_hours.py</code>).</p>"
            f"<table><tr><th>Business</th><th>Latest post and what Connect does</th></tr>{''.join(rows)}</table></div>")


# ---------------------------------------------------------------- server

class App(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, code=200):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def redirect(self, to):
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    def case(self, parts):
        try:
            eid, day = parts[1], date.fromisoformat(parts[2])
        except (IndexError, ValueError):
            return None
        if (eid, day.isoformat()) not in TARGET_ROWS:
            return None
        s = suggestion(eid, day)
        return None if s['action'] == 'SUGGEST' and not s['contact_eligible'] else s

    def do_GET(self):
        try:
            return self.handle_get()
        except YextError:
            return self.send(page('Service unavailable', '<h1>Could not verify saved hours</h1><p>No successful update is confirmed. Please try again when Yext is available.</p>'), 503)

    def handle_get(self):
        parts = [p for p in urllib.parse.urlparse(self.path).path.split("/") if p]
        if not parts:
            return self.send(connect_index())
        if parts == ["demo"]:
            if S5.applier.primary.name == 'sandbox':
                available = sorted(eid for eid in S5.applier.primary.mapping if eid in BL.ents)
                if not available:
                    return self.send(page('Demo unavailable', '<h1>No demo business is mapped</h1><p>Configure YEXT_SANDBOX_MIRROR with a supplied business ID, or restart with CONNECT_APPLY_MODE=demo for the local preview.</p>'), 503)
                available.sort(key=lambda eid: (eid != 'FI-D0001', eid))
            else:
                available = ['FI-D0017']
            for eid in available:
                pending = bundles_for(eid)
                if pending:
                    pid = next((pid for pid, _, _ in pending if pid == 'christmas'), pending[0][0])
                    return self.redirect(f'/w/{eid}/{pid}')
            return self.send(page('Demo complete', '<h1>All suggested opening hours are already saved</h1>'
                '<p>There are no remaining proposals for this demo. Existing hours have been kept.</p>'
                '<p>For a fresh walkthrough, restart with <code>python app/app.py --demo</code>. '
                'This starts a new local session without changing your Yext sandbox.</p>'))
        if parts == ["setup"]:
            return self.send(setup_page())
        if (parts[0] in ('w', 's', 'e', 'p', 'b', 'm', 'ig') and len(parts) >= 2
                and parts[1] in BL.ents and S5.applier.primary.name == 'sandbox'
                and parts[1] not in S5.applier.primary.mapping):
            return self.send(page('Business unavailable in sandbox',
                f"<h1>{h(BL.ents[parts[1]]['name'])} is a local demo business</h1>"
                "<p>This server uses the real Yext sandbox, where this business is not mapped. No hours were changed.</p>"
                "<p><a href='/demo'>Open the configured WhatsApp demo</a></p>"
                "<p>For the local preview of this business, restart with CONNECT_APPLY_MODE=demo.</p>"), 404)
        if parts[0] == "m":
            c = self.case(parts)
            if c is None:
                return self.send(page("Not found", "<p>Unknown business or date.</p>"), 404)
            if len(parts) == 4 and parts[3] == "edit" and c["action"] == "SUGGEST":
                return self.send(owner_edit(c))
            return self.send(owner_message(c))
        if parts == ["learning", "reset"]:
            LEARN.reset()
            NOREPLY.clear()
            EDIT_LEARNED.clear()
            return self.redirect("/")
        if parts[0] == "w" and len(parts) in (3, 4) and parts[2] == "welcome" and parts[1] in BL.ents:
            if len(parts) == 4 and parts[3] == "reset":
                WELCOME_CHAT.pop(parts[1], None)
                return self.redirect(f"/w/{parts[1]}/welcome")
            return self.send(welcome_page(parts[1]))
        if parts[0] in ("w", "s", "e", "p") and len(parts) in (3, 4) and parts[1] in BL.ents:
            ch, eid, pid = parts[:3]
            if is_check(eid, pid) and ch != 'w':
                return self.redirect(f'/b/{eid}/{pid}')

            if len(parts) == 4 and parts[3] == 'reset':
                PROPOSALS.pop((eid, pid), None)
                PENDING_HOURS.pop((ch, eid, pid), None)
                CHAT_TIMES.pop((ch, eid, pid), None)
            b = bundle(eid, pid)
            if len(parts) == 4:
                if parts[3] == "reset":
                    k = (ch, eid, pid)
                    REMIND.discard(k)
                    NOREPLY.discard(k)
                    EDIT_LEARNED.discard(k)
                    posts.POSTS.pop(f"{eid}~{pid}~hours", None)
                    for store in (WA, WA_CHAT, WA_DONE, WA_AUTO, WA_MORE):
                        store.pop(k, None)
                    SEEN.pop((eid, pid), None)
                    PROPOSALS.pop((eid, pid), None)
                    return self.redirect(f"/{ch}/{eid}/{pid}")
                if b and ch == "e" and parts[3] in ("yes", "no", "stop"):
                    return self.send(email_confirm(eid, *b, parts[3]))
            elif b:
                if ch == "w":
                    return self.send(wa_page(eid, *b))
                if ch == "s":
                    return self.send(sms_page(eid, *b, host=self.headers.get("Host") or f"localhost:{PORT}"))
                if ch == "e":
                    return self.send(email_page(eid, *b))
                return self.send(push_page(eid, *b))
            if not b and (eid, pid) not in LIVE.rows:
                return self.send(no_pending_proposal(eid))
        if parts[0] == "ig" and len(parts) in (2, 3) and parts[1] in social_hours.SAMPLES:   # Instagram hours
            if len(parts) == 3 and parts[2] == "reset":
                IG.pop(parts[1], None)
                SEEN.pop((parts[1], "instagram"), None)
                return self.redirect(f"/ig/{parts[1]}")
            if len(parts) == 2:
                return self.send(ig_page(parts[1]))
        if parts[0] == "wp" and len(parts) in (2, 3):                       # a post suggestion's own chat
            post_id = urllib.parse.unquote(parts[1])
            if len(parts) == 3 and parts[2] == "reset":
                posts.POSTS.pop(post_id, None)
                return self.redirect(f"/wp/{urllib.parse.quote(post_id)}")
            post = find_post(post_id)
            if post and len(parts) == 2:
                return self.send(post_page(post))
        if parts[0] == "c" and len(parts) == 2 and parts[1] in CODES:      # short link from an SMS
            return self.redirect(CODES[parts[1]])
        if parts[0] == "b" and len(parts) == 3 and parts[1] in BL.ents:
            b = bundle(parts[1], parts[2])
            if b:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                return self.send(bundle_page(parts[1], *b, via=(q.get("via") or ["WEB"])[0]))
        self.send(page("Not found", "<p>Not found.</p>"), 404)

    def post_bundle(self, eid, pid, act, form):
        b = bundle(eid, pid) if eid in BL.ents else None
        if b is None or act not in ("approve", "decline"):
            return self.send(page("Not found", "<p>Not found.</p>"), 404)
        _, title, items = b
        via = form.get("via") if form.get("via") in VIA else "WEB"
        label = LABEL[via] + (" (web page)" if via != "WEB" else "")
        ch = {"SMS": "s", "EMAIL": "e", "APP_PUSH": "p"}.get(via)    # the message this page was opened from
        if act == "decline":
            if ch:
                WA.setdefault((ch, eid, pid), "no")
            apply_via_step5(eid, pid, via, [(x, "REJECTED", None) for x in items])
            if items[0].get('live_check') or items[0].get('check'):
                LEARN.record(eid, day_keys(items), 'NO', pid, via if via != 'WEB' else None)
            DECISIONS[(eid, pid)] = f"{label}: declined"
            return self.send(declined_page(eid, title))
        results, bad = [], set()
        for s in items:
            k = s["day"].isoformat()
            choice = form.get(f"c_{k}", "sug")
            if choice == "custom":
                o, cl = form.get(f"o_{k}", ""), form.get(f"e_{k}", "")
                if not o or not cl or cl <= o:
                    bad.add(k)
                    continue
                results.append((s, f"{o}-{cl}", "edited"))
            elif choice == "closed":
                results.append((s, "closed", "edited" if s["hours"] != "closed" else "approved"))
            elif choice == "skip":
                results.append((s, None, "skipped"))
            else:
                results.append((s, s["hours"], "approved"))
        if bad:
            return self.send(bundle_page(eid, pid, title, items, form, bad))
        always = form.get("always") == "1" and not (items[0].get('live_check') or items[0].get('check'))
        if ch:
            WA.setdefault((ch, eid, pid), "other")
        res = apply_via_step5(eid, pid, via, [(x, {"approved": "ACCEPTED", "edited": "EDITED", "skipped": "REJECTED"}[w],
                                               hrs) for x, hrs, w in results], remember=always)
        if items[0].get('live_check') or items[0].get('check'):
            ok = any(r['status'] in ('APPLIED', 'ALREADY_SET') for r in res['items'])
            outcome = 'EDIT' if any(w == 'edited' for _, _, w in results) else 'YES'
            if ok or all(w == 'skipped' for _, _, w in results):
                LEARN.record(eid, day_keys(items), outcome if ok else 'NO', pid, via if via != 'WEB' else None)
        count = {w: sum(1 for r in results if r[2] == w) for w in ("approved", "edited", "skipped")}
        DECISIONS[(eid, pid)] = f"{label}: " + ", ".join(f"{v} {w}" for w, v in count.items() if v) + \
            (" · remember every year" if always else "")
        self.send(bundle_done(eid, title, [(x, hrs) for x, hrs, _ in results], always, res))

    def do_POST(self):
        try:
            return self.handle_post()
        except YextError:
            return self.send(page('Service unavailable', '<h1>Could not verify saved hours</h1><p>No successful update is confirmed. Please try again when Yext is available.</p>'), 503)

    def handle_post(self):
        if urllib.parse.urlparse(self.path).path in ('/messaging/inbound', '/messaging/status'):
            return self.messaging_webhook()
        parts = [p for p in urllib.parse.urlparse(self.path).path.split("/") if p]
        n = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8")).items()}
        if parts and parts[0] == "ig" and len(parts) in (2, 3) and parts[1] in social_hours.SAMPLES:
            if len(parts) == 2:
                ig_answer(parts[1], form.get("a", ""))
            elif parts[2] == "reply":
                ig_reply(parts[1], form.get("msg", ""))
            return self.redirect(f"/ig/{parts[1]}#end")
        if parts and parts[0] == "wp" and len(parts) in (2, 3):              # post suggestions
            post = find_post(urllib.parse.unquote(parts[1]))
            back = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("back") or [""])[0]
            here = back if back.startswith("/w/") else f"/wp/{parts[1]}"
            if post is None:
                return self.send(page("Not found", "<p>Not found.</p>"), 404)
            if len(parts) == 2:
                posts.act(post, form.get("a", ""))
                return self.redirect(here + "#end")
            if parts[2] == "reply":
                posts.reply(post, form.get("msg", ""))
                return self.redirect(here + "#end")
            if parts[2] == "days" and form.get("n", "").isdigit():       # demo clock for the monthly post
                posts.tick(post, int(form["n"]))
                return self.redirect("/")
        if parts == ["setup"]:
            return self.send(save_token(form.get("token", "")))
        if parts and parts[0] == "w" and len(parts) == 4 and parts[3] == "auto" and parts[1] in BL.ents:
            ch, eid, pid, ans = parts[0], parts[1], parts[2], form.get("a")
            k = (ch, eid, pid)
            auto = WA_AUTO.get(k)
            if auto and auto["state"] == "asked" and ans in ("yes", "no"):     # an answer counts once
                auto["state"] = ans
                chat = WA_CHAT.setdefault(k, [])
                chat.append(("me", AUTO_YES if ans == "yes" else AUTO_NO))
                if ans == "yes":
                    for key, hrs in auto["grants"]:
                        S5.applier.permissions.grant(eid, key, hrs, source=f"whatsapp-{pid}")
                    S5.applier.audit.append({"type": "PERMISSION_GRANTED", "entity_id": eid, "actor": "owner",
                                             "channel": "WHATSAPP", "days": [g[0] for g in auto["grants"]]})
                    chat.append(("them", "Great, thanks! Next year we'll set the same hours and send you a message "
                                         "when it's done.\n\nIf your plans change, just reply here."))
                    DECISIONS[(eid, pid)] = DECISIONS.get((eid, pid), "WhatsApp: yes") + " · handle it next year"
                else:
                    chat.append(("them", "No problem. We'll ask you first next time."))
                    DECISIONS[(eid, pid)] = DECISIONS.get((eid, pid), "WhatsApp: yes") + " · ask first next year"
            return self.redirect(f"/{ch}/{eid}/{pid}#end")
        if parts and parts[0] == "w" and len(parts) == 4 and parts[2] == "welcome" and parts[3] == "reply" \
                and parts[1] in BL.ents:
            eid, text = parts[1], form.get("msg", "").strip()
            if text:
                chat = WELCOME_CHAT.setdefault(eid, [])
                chat.append(("me", text))
                reply = stop_start_reply(eid, text)
                if reply is None:
                    undo = S5.handle_reply(eid, text, language="en")
                    reply = (undo["message"] or "There's nothing to undo yet.") if undo is not None else \
                        "Thanks! We'll be in touch when we have a suggestion for you."
                chat.append(("them", reply))
            return self.redirect(f"/w/{eid}/welcome#end")
        if parts and parts[0] == "w" and len(parts) == 4 and parts[3] == "more" and parts[1] in BL.ents:
            ch, eid, pid, ans = parts[0], parts[1], parts[2], form.get("a")
            k = (ch, eid, pid)
            more = WA_MORE.get(k)
            if more and more["state"] == "asked" and ans in ("yes", "no"):     # an answer counts once
                more["state"] = ans
                LEARN.set_wanted(eid, more["reasons"], keep=ans == "yes")
                chat = WA_CHAT.setdefault(k, [])
                chat.append(("me", MORE_YES if ans == "yes" else MORE_NO))
                if ans == "yes":
                    chat.append(("them", f"Great, we'll keep sending suggestions about {about(more['reasons'])}."))
                    DECISIONS[(eid, pid)] = DECISIONS.get((eid, pid), "WhatsApp: declined") + " · wants these in future"
                else:
                    chat.append(("them", f"OK, no more suggestions about {about(more['reasons'])}.\n\n"
                                         "If something looks clearly wrong on Google, we'll still let you know. "
                                         "Changed your mind? Reply START."))
                    DECISIONS[(eid, pid)] = DECISIONS.get((eid, pid), "WhatsApp: declined") + " · not these in future"
            return self.redirect(f"/{ch}/{eid}/{pid}#end")
        if len(parts) == 3 and parts[0] == "learning" and parts[2] == "silent" and parts[1] in BL.ents:
            LEARN.record(parts[1], [], "NO_REPLY", "demo")   # demo: one more unanswered message on the current channel
            return self.redirect("/")
        if parts and parts[0] == "w" and len(parts) == 4 and parts[3] == "noreply" and parts[1] in BL.ents:
            eid, pid = parts[1], parts[2]
            k, b = ("w", eid, pid), bundle(eid, pid)
            if b and k in REMIND and k not in WA and k not in NOREPLY:     # counted once, only after the reminder
                NOREPLY.add(k)
                LEARN.record(eid, day_keys(b[2]), "NO_REPLY", pid)
                DECISIONS[(eid, pid)] = "No reply after the reminder"
            return self.redirect("/")
        if parts and parts[0] in ("w", "s") and len(parts) == 4 and parts[3] == "reply" and parts[1] in BL.ents:
            ch, eid, pid, text = parts[0], parts[1], parts[2], form.get("msg", "").strip()
            b, k = bundle(eid, pid), (ch, eid, pid)
            # Owners can type at any time, like in the real WhatsApp. Free text goes to the AI model on OpenShift AI.
            if b is None or not text:
                return self.redirect(f"/{ch}/{eid}/{pid}#end")
            hp = posts.POSTS.get(f"{eid}~{pid}~hours") if ch == "w" else None
            if posts.wants_text(hp, text):                 # new text for the post, or DELETE after publishing
                posts.reply(hp, text)
                return self.redirect(f"/{ch}/{eid}/{pid}#end")
            chat = WA_CHAT.setdefault(k, [])
            chat.append(("me", text))
            label = LABEL[CH_API[ch]]

            def say(msg):                                   # SMS: keep to the plain GSM alphabet
                chat.append(("them", sms_safe(msg) if ch == "s" else msg))
            word = text.lower().strip(" .!")
            if k in PENDING_HOURS and word in TYPED_YES + TYPED_NO:
                word = 'confirm' if word in TYPED_YES else 'cancel'
                text = word.upper()
            # Polite replies get a polite answer. (On SMS, an "OK" before answering still means yes.)
            if is_polite(text) and not (ch == "s" and k not in WA and word in ("ok", "okay")):
                say(POLITE_REPLY)
                return self.redirect(f"/{ch}/{eid}/{pid}#end")
            if ch == "s" and word in ("yes", "y", "ok", "no", "n"):      # SMS has no buttons: YES / NO as words
                if k in WA:
                    say("You've already answered this one. To change a day, send it like "
                        f"\"{b[2][0]['day'].day} {b[2][0]['day']:%b} 10-14\". Reply UNDO to put back the latest change.")
                else:
                    WA[k] = "yes" if word in ("yes", "y", "ok") else "no"
                    res = answer(ch, eid, pid, b[2], WA[k])
                    say(NO_TEXT if WA[k] == "no" else (without_where(res["message_short"], eid) or without_where(res["message"], eid)
                                                       or "Your hours are already up to date. Nothing needed changing."))
                return self.redirect(f"/{ch}/{eid}/{pid}#end")
            if ch == "w" and k not in WA and word in TYPED_YES + TYPED_NO:   # typed instead of tapping a button
                wa_answer(eid, pid, b, "yes" if word in TYPED_YES else "no", typed=True)
                return self.redirect(f"/{ch}/{eid}/{pid}#end")
            if text.lower().strip(" .!") in ASK_ME_WORDS:   # owner takes back "handle it for me"
                if revoke_auto(k, eid):
                    DECISIONS[(eid, pid)] = DECISIONS.get((eid, pid), "").replace("handle it next year", "ask first next year")
                say("OK! We'll always ask you first before changing your hours.")
                return self.redirect(f"/{ch}/{eid}/{pid}#end")
            reply = stop_start_reply(eid, text)
            if reply is not None:
                say(reply)
                return self.redirect(f"/{ch}/{eid}/{pid}#end")
            undo = S5.handle_reply(eid, text, language="en")      # "UNDO" -> step 5 puts the latest change back
            if undo is not None:
                PENDING_HOURS.pop(k, None)
                undo = browser_result(undo)
                if any(x['status'] == 'UNDONE' for x in undo.get('items', [])):
                    SEEN[(eid, pid)] = S5.snapshot(eid, [x['day'].isoformat() for x in b[2]])
                DELIVERY[(eid, pid)] = undo
                msg = undo["message"] or "There's nothing to undo."
                if undo["message"] and revoke_auto(k, eid):    # the hours were wrong: don't repeat them next year
                    msg += "\n\nWe'll also ask you first next year."
                if undo["message"]:
                    LEARN.record(eid, day_keys(b[2]), "UNDO", pid, CH_API[ch])   # UNDO counts as a "no" for these reasons
                say(msg)
                DECISIONS[(eid, pid)] = f"{label}: undone"
                return self.redirect(f"/{ch}/{eid}/{pid}#end")
            if word == 'cancel':
                PENDING_HOURS.pop(k, None)
                say('Cancelled. Your existing hours stay unchanged.')
                return self.redirect(f"/{ch}/{eid}/{pid}#end")
            if word == 'confirm':
                found = PENDING_HOURS.pop(k, None)
                if not found:
                    say('There is no draft to confirm. Send the dates and hours you want to change.')
                    return self.redirect(f"/{ch}/{eid}/{pid}#end")
            else:
                PENDING_HOURS.pop(k, None)
                found, problems = parse_hours_reply(text, b[2])
                READER[k] = 'OpenShift AI' if hours_ai.configured() else 'exact date/time pattern reader (AI not configured)'
                if problems or not found:
                    say('Nothing has been saved. ' + ('; '.join(problems) if problems else
                        'Please write one day per line, for example "24 Dec 10-14" or "25 Dec closed".'))
                    return self.redirect(f"/{ch}/{eid}/{pid}#end")
                PENDING_HOURS[k] = found
                WA.setdefault(k, "other")
                lines = '\n'.join(f"• {d.day} {d:%b}: {state(hrs, 'en')}" for d, hrs in sorted(found.items()))
                say(f"Please review these hours:\n{lines}\n\nOther dates stay unchanged. Reply CONFIRM to save or CANCEL to discard.")
                return self.redirect(f"/{ch}/{eid}/{pid}#end")
            by_day = {x["day"]: x for x in b[2]}
            res = apply_via_step5(eid, pid, CH_API[ch], [(by_day[d], "EDITED", hrs) for d, hrs in sorted(found.items())])
            ok = {it["date"] for it in res["items"] if it["status"] in ("APPLIED", "ALREADY_SET")}
            saved = "\n".join(f"• {by_day[d]['weekday'][:3]} {by_day[d]['dm']}: {state(hrs, 'en')}"
                              for d, hrs in sorted(found.items()) if d.isoformat() in ok)
            # Team decision: this follow-up confirmation doesn't list where the hours show.
            msg = f"Thanks! We saved your hours:\n{saved}" if saved else "Thanks!"
            rest = len(b[2]) - len(found)
            if rest:
                msg += f"\n\nThe other {rest} days stay as they are." if rest > 1 else "\n\nThe other day stays as it is."
            for note in problems_text(res):
                msg += "\n\n" + note
            if saved:
                msg += "\n\nWrong? Reply UNDO and we'll put it back."
            say(msg)
            WA.setdefault(k, "other")                      # SMS: typed hours count as the answer
            if ok and k not in EDIT_LEARNED:
                EDIT_LEARNED.add(k)
                LEARN.record(eid, day_keys(b[2]), "EDIT", pid, CH_API[ch])
            DECISIONS[(eid, pid)] = (f"{label}: own hours for {len(ok)} day{'s' if len(ok) != 1 else ''}"
                                     f" · read by {READER.get(k, 'confirmed draft')}")
            return self.redirect(f"/{ch}/{eid}/{pid}#end")
        if parts and parts[0] in ("e", "p") and len(parts) == 3 and parts[1] in BL.ents:
            ch, eid, pid, ans = parts[0], parts[1], parts[2], form.get("a")
            b, k = bundle(eid, pid), (parts[0], parts[1], parts[2])
            if b is None or ans not in (("yes", "no", "stop") if ch == "e" else ("yes", "no", "undo")):
                return self.send(page("Not found", "<p>Not found.</p>"), 404)
            if ans == "undo":                 # the Undo button on the push confirmation
                if k in WA:
                    undo = S5.handle_reply(eid, "UNDO", language="en")
                    if undo is not None:
                        DELIVERY[(eid, pid)] = undo
                    WA_CHAT.setdefault(k, []).append(("them", (undo or {}).get("message") or "There's nothing to undo."))
                    DECISIONS[(eid, pid)] = "App push: undone"
                return self.redirect(f"/p/{eid}/{pid}")
            if k in WA:                       # an answer counts once
                return self.redirect(f"/{ch}/{eid}/{pid}")
            WA[k] = ans
            if ans == "stop":
                if b[2][0].get('live_check'):
                    LEARN.set_wanted(eid, ['event'], keep=False)
                    DECISIONS[(eid, pid)] = 'Email: stopped event checks'
                    return self.send(page(BL.ents[eid]['name'], "<p>Event checks are off. Your hours stay as they are.</p>"))
                DECISIONS[(eid, pid)] = "Email: stopped suggestions"
                return self.send(page(BL.ents[eid]["name"],
                                      "<div class='brand'><span class='dot'></span>Fonecta Connect</div><div class='card'>"
                                      "<h2>Done. You won't get holiday suggestions any more.</h2><p class='muted'>Your hours "
                                      "stay as they are. You can turn suggestions back on in Fonecta Connect.</p></div>"
                                      "<p class='muted' style='text-align:center;margin-top:14px'><a href='/'>← Connect demo panel</a></p>"))
            res = answer(ch, eid, pid, b[2], ans)
            if ch == "p":                     # the reply comes back as a new notification
                msg = NO_TEXT if ans == "no" else (without_where(res["message_short"], eid) or without_where(res["message"], eid)
                                                   or "Your hours are already up to date. Nothing needed changing.")
                WA_CHAT[k] = [("them", msg)]
                return self.redirect(f"/p/{eid}/{pid}")
            if ans == "no":
                return self.send(declined_page(eid, b[1]))
            return self.send(bundle_done(eid, b[1], [(x, x["hours"]) for x in b[2]], False, res))
        if parts and parts[0] == "w" and len(parts) == 4 and parts[3] == "remind" and parts[1] in BL.ents:
            eid, pid = parts[1], parts[2]
            if (eid, pid) in LIVE.rows:
                return self.redirect(f'/w/{eid}/{pid}')
            if bundle(eid, pid) and ("w", eid, pid) not in WA:     # only if the owner hasn't answered
                REMIND.add(("w", eid, pid))
                DECISIONS[(eid, pid)] = "WhatsApp: reminder sent (no reply after 24 h)"
            return self.redirect(f"/w/{eid}/{pid}#end")
        if parts and parts[0] == "w" and len(parts) == 3 and parts[1] in BL.ents:
            ch, eid, pid, ans = parts[0], parts[1], parts[2], form.get("a")
            b = bundle(eid, pid)
            if b is None or ans not in ("yes", "no", "other"):
                return self.send(page("Not found", "<p>Not found.</p>"), 404)
            k = (ch, eid, pid)
            if k not in WA:                   # an answer counts once
                wa_answer(eid, pid, b, ans)
            return self.redirect(f"/{ch}/{eid}/{pid}#end")
        if parts and parts[0] == "b" and len(parts) == 4:
            return self.post_bundle(parts[1], parts[2], parts[3], form)
        c = self.case(parts) if parts and parts[0] == "m" else None
        if c is None or len(parts) != 4 or c["action"] != "SUGGEST":
            return self.send(page("Not found", "<p>Not found.</p>"), 404)
        k, always = (c["eid"], c["day"].isoformat()), form.get("always") == "1"
        if parts[3] == "decline":
            apply_via_step5(c["eid"], c["day"].isoformat(), "WEB", [(c, "REJECTED", None)])
            DECISIONS[k] = "Declined"
            return self.send(owner_declined(c))
        if parts[3] == "approve":
            hours = c["hours"]
            DECISIONS[k] = "Approved"
        elif parts[3] == "edit":
            if form.get("mode") == "closed":
                hours = "closed"
            else:
                o, cl = form.get("open", ""), form.get("close", "")
                if not o or not cl or cl <= o:
                    return self.send(owner_edit(c, UI[c["lang"]]["bad_time"]))
                hours = f"{o}-{cl}"
            DECISIONS[k] = f"Edited to {nice(hours) if hours != 'closed' else 'closed'}"
        else:
            return self.send(page("Not found", "<p>Not found.</p>"), 404)
        if always:
            DECISIONS[k] += " · remember every year"
        res = apply_via_step5(c["eid"], c["day"].isoformat(), "WEB",
                        [(c, "ACCEPTED" if parts[3] == "approve" else "EDITED", hours)], remember=always)
        verified = any(x['status'] in ('APPLIED', 'ALREADY_SET') for x in res.get('items', []))
        DECISIONS[k] = ('Verified approval' if parts[3] == 'approve' else 'Verified edit') if verified else 'Not saved; review required'
        self.send(owner_done(c, hours, always, res))


    def messaging_webhook(self):
        code = 200
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 65536 or self.headers.get_content_type() != "application/x-www-form-urlencoded":
                raise ValueError("Invalid webhook body")
            raw_body = self.rfile.read(size)
            if MESSAGING is None:
                code = 503
            else:
                MESSAGING.webhook(self.path, raw_body, self.headers.get("X-Twilio-Signature", ""))
        except PermissionError:
            code = 403
        except (ValueError, UnicodeError):
            code = 400
        except Exception:
            code = 503  # Twilio can retry; no message is acknowledged before durable storage.
        raw = b"<Response></Response>" if code == 200 else b"Webhook unavailable or invalid"
        self.send_response(code)
        self.send_header("Content-Type", "text/xml; charset=utf-8" if code == 200 else "text/plain")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

if __name__ == "__main__":
    import argparse
    import json
    import threading
    parser = argparse.ArgumentParser()
    parser.add_argument('--demo', action='store_true', help='Fresh local browser walkthrough; keeps configured AI parsing, disables Yext and external message delivery')
    parser.add_argument("--send-whatsapp", nargs=2, metavar=("BUSINESS", "PERIOD"))
    parser.add_argument("--messaging-status", action="store_true")
    parser.add_argument('--check-services', action='store_true', help='Read-only Yext and synthetic OpenShift inference check; never sends WhatsApp or writes hours')
    args = parser.parse_args()
    if args.demo and args.send_whatsapp:
        parser.error('--demo cannot send real WhatsApp messages; omit --demo for provider delivery')
    MESSAGING, cfg = initialize_messaging()
    if args.check_services:
        ai = {'configured': hours_ai.configured(), 'verified': False, 'status': hours_ai.status()}
        if ai['configured']:
            found, problems = hours_ai.read_reply('On Christmas Eve we open at noon and close at 6pm',
                                                 [{'day': date(2026, 12, 24), 'day_name': 'Christmas Eve'}])
            ai['verified'] = not problems and found == {date(2026, 12, 24): '12:00-18:00'}
        sandbox = S5.applier.primary.name == 'sandbox'
        sandbox_verified = False
        if sandbox:
            try:
                for eid in S5.applier.primary.mapping:
                    S5.snapshot(eid, [])
                sandbox_verified = True
            except (YextError, OSError, ValueError):
                pass
        print(json.dumps({'ai': ai, 'apply_target': 'sandbox' if sandbox else 'demo',
                          'sandbox_read_verified': sandbox_verified,
                          'apply_status': S5.selection_reason, 'messaging': cfg,
                          'prediction_source': str(PREDICTIONS.path),
                          'prediction_sha256': PREDICTIONS.sha256}, indent=2))
        S5.close()
        raise SystemExit(0 if ai['verified'] and sandbox_verified else 2)
    if args.messaging_status:
        print(json.dumps({"configuration": cfg, "state": MESSAGING.status() if MESSAGING else None}, indent=2))
        S5.close()
        raise SystemExit(0)
    if args.send_whatsapp:
        if MESSAGING is None:
            eid, pid = args.send_whatsapp
            print(json.dumps({"mode": "demo", "status": "preview_ready", "sent": False,
                              "preview_url": f"http://localhost:{PORT}/w/{urllib.parse.quote(eid)}/{urllib.parse.quote(pid)}",
                              "reason": cfg["reason"]}))
            S5.close()
            raise SystemExit(0)
        try:
            print(json.dumps(MESSAGING.send_proposal(*args.send_whatsapp)))
        except Exception:
            print("Message was not confirmed as submitted. Check --messaging-status and operator setup; do not blindly resend.", file=sys.stderr)
            raise SystemExit(2)
        finally:
            S5.close()
        raise SystemExit(0)
    if MESSAGING:
        threading.Thread(target=MESSAGING.run, name="real-whatsapp", daemon=True).start()
    else:
        print("Real WhatsApp is unconfigured; browser chats are local previews.")
    threading.Thread(target=lambda: search_value._load(MAT), daemon=True).start()
    print(f"Owner page running: http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), App).serve_forever()
