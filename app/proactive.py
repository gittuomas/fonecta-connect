"""Proactive checks beyond the holiday calendar: weather and season (see docs/contact-step-plan.md).

Which businesses get them comes from the challenge data, not guesses:

- Weather -> snow-dependent businesses. On special days 2024-2026 (labels/dev_truth_history.csv) ski rentals were
  closed 100% of the time without snow and 6% of the time with 10 cm or more. No other business type comes close
  (the next biggest gap is ~15 points, which is just winter vs. other seasons). Google views barely move with the
  weather for any type, so we don't send weather messages to cafés or restaurants "just because".
- Season -> businesses with a seasonal break. All 40 businesses whose published hours history has a winter or
  summer break are seasonal terraces (close 1 Oct, reopen 1 May: 112 of 112 breaks) or ski rentals (close 1 May,
  reopen 1 Nov). 10 of the 38 terraces forgot to publish their break this year, e.g. FI-D0006.

How proactive is learned per owner (learning.py, reason "weather" / "season"): an owner who acts on weather
messages gets them for smaller snowfalls too; one who ignores them only for big ones; "No, not these" turns them off.
Standard library only.
"""
from __future__ import annotations

import csv
import json
import math
import os
from collections import Counter, defaultdict
from datetime import date, timedelta
from functools import lru_cache

HERE = os.path.dirname(os.path.abspath(__file__))
MAT = os.path.join(os.path.dirname(HERE), "resources")

SNOW_DEPENDENT = {"ski_rental"}
SNOW_OPEN_CM = 10            # ski rentals open on 94% of special days with this much snow, 0% below it
NEW_SNOW_CM = {"acts": 5, "neutral": 10, "ignores": 20}   # fresh snow that triggers a message, by owner score
ASK_DAYS_BEFORE_CLOSE = 21   # "Summer is ending: are you closing?" three weeks before the usual date
DEFAULT_BREAK = {"seasonal_terrace": ("10-01", "05-01"), "ski_rental": ("05-01", "11-01")}


def today() -> date:
    """Demo 'today'. CONNECT_TODAY overrides; otherwise kept inside the weather forecast window."""
    if os.environ.get("CONNECT_TODAY"):
        return date.fromisoformat(os.environ["CONNECT_TODAY"])
    first, last = _forecast_window()
    t = date.today()
    return min(max(t, first), last - timedelta(days=1)) if first else t


# ------------------------------------------------------------------ data
@lru_cache(maxsize=1)
def _weather():
    out = defaultdict(dict)   # city -> date -> row
    with open(os.path.join(MAT, "data", "weather_daily.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[r["city"]][date.fromisoformat(r["date"])] = r
    return out


@lru_cache(maxsize=1)
def _forecast_window():
    days = sorted({d for rows in _weather().values() for d, r in rows.items() if r["is_forecast"] == "1"})
    return (days[0], days[-1]) if days else (None, None)


@lru_cache(maxsize=1)
def _breaks():
    """eid -> (close 'MM-DD', reopen 'MM-DD') from the published hours history."""
    seen = defaultdict(Counter)
    with open(os.path.join(MAT, "data", "regular_hours_history.jsonl"), encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r["hours"].get("reopenDate") and r["effective_from"][:4].isdigit():
                seen[r["entity_id"]][(r["effective_from"][5:], r["hours"]["reopenDate"][5:])] += 1
    return {eid: c.most_common(1)[0][0] for eid, c in seen.items()}


def season_break(eid: str, ent: dict):
    """(close MM-DD, reopen MM-DD) for a seasonal business, else None."""
    return _breaks().get(eid) or DEFAULT_BREAK.get(ent.get("c_segment"))


def _md(year: int, md: str) -> date:
    return date(year, int(md[:2]), int(md[3:]))


def _nice(hours: str) -> str:
    if hours == "closed":
        return "closed"
    def t(x):
        hh, mm = x.split(":")
        return str(int(hh)) + ("" if mm == "00" else ":" + mm)
    return ", ".join("–".join(t(x) for x in iv.split("-")) for iv in hours.split(","))


# ------------------------------------------------------------------ weather
def weather_check(eid: str, ent: dict, displayed, score: float = 0.0, on: bool = True,
                  forecast=None, decision_date=None):
    """One weather message for a snow-dependent business, or None.
    displayed(day) -> what Google shows that day ('closed' or '09:00-17:00')."""
    if not on or ent.get("c_segment") not in SNOW_DEPENDENT:
        return None
    live = forecast is not None
    rows = forecast if live else _weather().get(ent["address"]["city"], {})
    t = decision_date or today()

    def number(value):
        try:
            n = float(value)
            return n if math.isfinite(n) and n >= 0 else None
        except (TypeError, ValueError):
            return None
    level = "acts" if score > 0 else ("ignores" if score < 0 else "neutral")
    need = NEW_SNOW_CM[level]
    best = None
    for d in sorted(rows):
        if d <= t or rows[d]["is_forecast"] != "1":
            continue
        prev = rows.get(d - timedelta(days=1))
        depth = number(rows[d].get('snow_depth_cm'))
        if depth is None:
            continue
        # Daily snowfall is a different measurement from change in settled snow depth.
        # Keep the legacy synthetic demo's proxy only when no live replacement is configured.
        new = number(rows[d].get('snowfall_cm')) if live else depth - float(prev['snow_depth_cm']) if prev else 0.0
        shown = displayed(d)
        if shown == "closed":
            continue
        if new is not None and new >= need and (best is None or best[3] != 'snow' or new > best[1]):
            best = (d, new, depth, "snow")
        elif depth < SNOW_OPEN_CM and best is None:
            best = (d, new, depth, "no_snow")
    if not best:
        return None
    d, new, depth, kind = best
    city = ent["address"]["city"].split(" (")[-1].rstrip(")")
    shown = displayed(d)
    when = f"{d:%a} {d.day} {d:%b}"
    if kind == "snow":
        intro = (f"Fresh snow in {city}: about {int(new)} cm of new snow is forecast for {when} "
                 f"({int(depth)} cm on the ground)." + ("" if live else " Ski rentals are almost always open when there's snow like this."))
        question = "Will you be open as usual?"
        labels = {"yes": "Yes, open as usual", "no": "Not relevant", "other": "Other hours"}
        why = f"{int(new)} cm new snow forecast (threshold for this owner: {need} cm, {level})"
    else:
        intro = (f"Hardly any snow in {city} yet: only about {int(depth)} cm is forecast for {when}."
                 + ("" if live else f" Ski rentals are usually closed until there's at least {SNOW_OPEN_CM} cm."))
        question = "Are you open that day?"
        labels = {"yes": "Yes, we're open", "no": "Not relevant", "other": "Other hours"}
        why = f"only {int(depth)} cm snow forecast"
    return {"pid": f"weather-{d.isoformat()}", "title": "Snow" if kind == "snow" else "No snow yet", "day": d,
            "key": "WEATHER", "shown": shown, "hours": shown, "kind": "weather", "confirm": True,
            "intro": intro, "msg": f"{intro}\n\nGoogle shows you open {_nice(shown)} that day.",
            "question": question, "labels": labels, "why": why,
            "source": 'Open-Meteo live forecast' if live else 'Challenge weather fixture',
            "no_reply": "OK, nothing changes. Google keeps showing your regular hours.",
            "timeline": "Sent the day before at the owner's send time · short notice, so no reminder or call"}


# ------------------------------------------------------------------ season
def season_check(eid: str, ent: dict, listed_break: bool, score: float = 0.0, on: bool = True):
    """'Summer is ending: are you closing for the winter?' or None.
    listed_break: the listing already shows the break (hours.reopenDate)."""
    br = season_break(eid, ent)
    if not on or not br or listed_break:
        return None
    t = today()
    close_md, reopen_md = br
    close = _md(t.year, close_md)
    if close > t + timedelta(days=ASK_DAYS_BEFORE_CLOSE):
        close = _md(t.year - 1, close_md)
    reopen = _md(close.year, reopen_md)
    if reopen <= close:
        reopen = _md(close.year + 1, reopen_md)
    if not (close - timedelta(days=ASK_DAYS_BEFORE_CLOSE) <= t < reopen):
        return None
    off = "winter" if close.month >= 7 else "summer"
    on_season = "summer" if off == "winter" else "winter"
    from_history = eid in _breaks()
    closes = f"{close.day} {close:%b}"
    reopens = f"{reopen.day} {reopen:%b}"
    if t < close:
        intro = (f"The {on_season} season is ending soon. "
                 + (f"Last year you closed for the {off} on {closes} and reopened on {reopens}." if from_history
                    else f"Seasonal places like yours usually close for the {off} on {closes}."))
        question = f"Are you closing for the {off} again?"
        why = f"Usual {off} break from {closes}; not on Google yet"
    else:
        intro = (f"The {on_season} season is over, but Google still shows you open every week. "
                 f"Customers may come to a closed door.")
        question = f"Are you closed for the {off}?"
        why = f"Past the usual closing date ({closes}) and Google still shows regular hours"
    return {"pid": "season", "title": f"Closing for the {off}", "kind": "season", "confirm": False,
            "close": close, "reopen": reopen, "intro": intro, "question": question, "why": why,
            "labels": {"yes": "Yes, we're closed", "no": "No, we're open", "other": "Other hours"},
            "no_reply": "Great, nothing changes. Google keeps showing your regular hours.",
            "timeline": "Sent now at the owner's send time → WhatsApp reminder after 1 day → stop"}
