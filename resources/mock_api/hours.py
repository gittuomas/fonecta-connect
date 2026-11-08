"""Opening-hours primitives shared by the generator, the mock API and the scorer.

Canonical text form ("hours string"):
    "closed"                       -> closed all day
    "09:00-17:00"                  -> one interval
    "10:00-14:00,17:00-22:00"      -> split day
    "18:00-02:00"                  -> past midnight (end < start)
    "00:00-23:59"                  -> open 24 hours (Yext convention)
"""
from __future__ import annotations

import re

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_ABBR = {"mo": 0, "tu": 1, "we": 2, "th": 3, "fr": 4, "sa": 5, "su": 6}


def t2m(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def m2t(m: int) -> str:
    m = int(m) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def parse_week(spec: str) -> list[list[tuple[int, int]]]:
    """'mo-fr 09:00-17:00; sa 10:00-14:00' -> 7 lists of (start,end) minutes.

    end may exceed 1440 for intervals that run past midnight.
    """
    week: list[list[tuple[int, int]]] = [[] for _ in range(7)]
    for part in [p.strip() for p in spec.split(";") if p.strip()]:
        days, *ivs = part.split()
        if "-" in days:
            a, b = days.split("-")
            idx = list(range(_ABBR[a], _ABBR[b] + 1))
        else:
            idx = [_ABBR[d] for d in days.split(",")]
        for iv in ",".join(ivs).split(","):
            if not iv:
                continue
            s, e = iv.split("-")
            sm, em = t2m(s), t2m(e)
            if em <= sm and not (s == "00:00" and e == "23:59"):
                em += 1440
            for d in idx:
                week[d].append((sm, em))
    return week


def ivs_to_str(ivs: list[tuple[int, int]]) -> str:
    if not ivs:
        return "closed"
    return ",".join(f"{m2t(s)}-{m2t(e) if e != 1439 else '23:59'}" for s, e in ivs)


def str_to_ivs(s: str) -> list[tuple[int, int]]:
    s = (s or "").strip().lower()
    if s in ("", "closed"):
        return []
    out = []
    for iv in s.split(","):
        a, b = iv.strip().split("-")
        sm, em = t2m(a), t2m(b)
        if em <= sm and not (a == "00:00" and b == "23:59"):
            em += 1440
        out.append((sm, em))
    return out


HOURS_RE = re.compile(r"^(closed|\d{2}:\d{2}-\d{2}:\d{2}(,\d{2}:\d{2}-\d{2}:\d{2})*)$")


def is_valid_hours(s: str) -> bool:
    if not isinstance(s, str) or not HOURS_RE.match(s.strip().lower()):
        return False
    try:
        for a, b in (iv.split("-") for iv in s.split(",")) if s != "closed" else []:
            for t in (a, b):
                h, m = map(int, t.split(":"))
                if h > 23 or m > 59:
                    return False
    except ValueError:
        return False
    return True


def week_to_yext(week) -> dict:
    out = {}
    for i, d in enumerate(DAYS):
        ivs = week[i]
        if not ivs:
            out[d] = {"isClosed": True}
        else:
            out[d] = {"openIntervals": [{"start": m2t(s), "end": "23:59" if e == 1439 else m2t(e)} for s, e in ivs]}
    return out


def hours_str_to_yext_day(s: str) -> dict:
    ivs = str_to_ivs(s)
    if not ivs:
        return {"isClosed": True}
    return {"openIntervals": [{"start": m2t(a), "end": "23:59" if b == 1439 else m2t(b)} for a, b in ivs]}


def yext_day_to_str(d: dict) -> str:
    if not d or d.get("isClosed"):
        return "closed"
    ivs = d.get("openIntervals") or []
    if not ivs:
        return "closed"
    return ",".join(f"{iv['start']}-{iv['end']}" for iv in ivs)


def yext_to_week(h: dict) -> list[list[tuple[int, int]]]:
    return [str_to_ivs(yext_day_to_str(h.get(d, {"isClosed": True}))) for d in DAYS]


def same_state(a: str, b: str) -> bool:
    return (a == "closed") == (b == "closed")


def boundary_diff(a: str, b: str) -> int:
    """Max absolute minute difference between first open / last close. Large if shapes differ."""
    ia, ib = str_to_ivs(a), str_to_ivs(b)
    if not ia or not ib:
        return 0 if (not ia and not ib) else 9999
    return max(abs(ia[0][0] - ib[0][0]), abs(ia[-1][1] - ib[-1][1]))
