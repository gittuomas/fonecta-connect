"""One short, true line that tells the owner why the hours matter, from their OWN Google numbers.

Team decision, in this order (first one that fits):
  1. Lots of views (30+ on the busiest day):  "Last Christmas Eve, about 230 people looked you up on Google."
  2. Only a few: whichever is more urgent of
       calls (2+ on a day):                    "Last Christmas Eve, 3 customers called you."
       the whole period (20+ views in total):  "Over last Christmas, 45 people looked you up on Google."
  3. A clear holiday jump (1.5x a normal day): "Last Christmas, twice as many people as usual looked you up on Google."
  4. Otherwise no number, only the general reason.

Honesty rules: only this business's own data (never other businesses'), last year's facts only,
numbers are rounded DOWN ("about 47" -> "about 45"), "looked you up" = Google search + map views,
"called you" = calls from Google.

Data: data/analytics_daily.parquet from the challenge package (needs pandas + pyarrow).
"""
from __future__ import annotations

import os
import statistics
from datetime import date, timedelta

MANY_VIEWS = 30        # rule 1: a day with this many views is "lots"
MIN_CALLS = 2          # rule 2: calls worth mentioning
MIN_TOTAL = 20         # rule 2: views over the whole period worth mentioning
MIN_JUMP = 1.5         # rule 3: holiday vs normal day

_DATA = None


def _load(mat_dir):
    """{entity_id: {date: (views, calls)}}, loaded once."""
    global _DATA
    if _DATA is None:
        import pandas as pd
        df = pd.read_parquet(os.path.join(mat_dir, "data", "analytics_daily.parquet"),
                             columns=["date", "entity_id", "GOOGLE_SEARCH_VIEWS", "GOOGLE_MAP_VIEWS", "GOOGLE_PHONE_CALLS"])
        data = {}
        for eid, d, sv, mv, calls in zip(df["entity_id"], df["date"].astype(str), df["GOOGLE_SEARCH_VIEWS"],
                                         df["GOOGLE_MAP_VIEWS"], df["GOOGLE_PHONE_CALLS"]):
            data.setdefault(eid, {})[date.fromisoformat(d)] = (int(sv) + int(mv), int(calls))
        _DATA = data
    return _DATA


def round_down(n):
    """Never round up: 47 -> 45, 232 -> 230, 1260 -> 1250. Small numbers stay exact."""
    if n < 20:
        return n
    step = 5 if n < 100 else (10 if n < 1000 else 50)
    return n // step * step


def _about(n):
    r = round_down(n)
    return f"about {r}" if r != n else str(n)


def _times(ratio):
    if ratio >= 3:
        return "three times as many people as usual"
    if ratio >= 2:
        return "twice as many people as usual"
    return f"{int((ratio - 1) * 100) // 10 * 10}% more people than usual"


def last_year_date(bl, eid, s):
    """The same special day last year (by the challenge calendar), or None."""
    from baseline import cycle   # noqa: PLC0415 (challenge helper: Feb-Jan holiday "season")
    if s["key"] in ("EVENT",):
        return None              # last year's events were different events
    if s["key"] == "SCHOOL_BREAK":
        return s["day"] - timedelta(days=364)   # same weekday, one year back
    country = bl.ents[eid]["meta"]["countryCode"]
    prev = [x for x in bl.dates_by_key[(country, s["key"])] if cycle(x) == cycle(s["day"]) - 1]
    return prev[0] if prev else None


def _on(day):
    """'Last Christmas Eve' for named holidays; 'On 23 Dec last year' for days without a proper name."""
    s, ly = day[0], day[1]
    if s["key"] in ("DEC23", "INTERDAY", "SCHOOL_BREAK") or s["day_name"].lower().startswith(("day before", "between")):
        return f"On {ly.day} {ly:%b} last year"
    return f"Last {s['day_name']}"


def value_line(mat_dir, bl, eid, pid, title, items):
    """The line for this message, or the general reason when no number is both true and convincing."""
    data = _load(mat_dir).get(eid, {})
    days = []   # (suggestion, last year's date, views, calls)
    for s in items:
        ly = last_year_date(bl, eid, s)
        if ly and ly in data:
            days.append((s, ly) + data[ly])
    period = pid in ("christmas", "new-year")
    general = ("Many people check opening hours on Google before the holidays. "
               "If they're wrong, customers may find a closed door.") if not items[0].get("event") else (
               "Many people check opening hours on Google before going out. "
               "If they're wrong, customers may find a closed door.")
    if not days:
        return general

    busiest = max(days, key=lambda d: d[2])
    if busiest[2] >= MANY_VIEWS:                                   # 1. lots of views
        return f"{_on(busiest)}, {_about(busiest[2])} people looked you up on Google."

    options = []                                                    # 2. a few: calls or the period total
    callday = max(days, key=lambda d: d[3])
    if callday[3] >= MIN_CALLS:
        options.append((callday[3] * 10, f"{_on(callday)}, {callday[3]} customers called you."))
    total = sum(d[2] for d in days)
    if total >= MIN_TOTAL:
        when = f"Over last {title}" if period and len(days) > 1 else _on(busiest)
        options.append((total, f"{when}, {_about(total)} people looked you up on Google."))
    if options:
        return max(options)[1]

    first = min(d[1] for d in days)                                 # 3. the holiday jump
    normal = [data[first - timedelta(days=k)][0] for k in range(7, 35) if first - timedelta(days=k) in data]
    if len(normal) >= 7 and statistics.median(normal) > 0:
        ratio = busiest[2] / statistics.median(normal)
        if ratio >= MIN_JUMP:
            when = f"Last {title}" if period else _on(busiest)
            return f"{when}, {_times(ratio)} looked you up on Google."
    return general                                                  # 4. no number
