"""Opening-hours strings <-> Yext hours objects, plus validation that mirrors the Yext rules.

Hours strings (same format as the challenge data):
    "closed" · "regular" (= isRegularHours) · "10:00-15:00" · "10:00-14:00,17:00-22:00" · "18:00-02:00" · "00:00-23:59"
"""
from __future__ import annotations

import copy
import re

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
CLOSED, REGULAR = "closed", "regular"
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def normalise(s: str) -> str:
    """Tolerate what people type: spaces, en dashes, '9:00'. Returns the canonical string (may still be invalid)."""
    s = (s or "").strip().lower().replace(" ", "").replace("–", "-").replace("—", "-")
    out = []
    for iv in s.split(","):
        parts = iv.split("-")
        if len(parts) == 2:
            parts = [("0" + p if re.match(r"^\d:\d\d$", p) else p) for p in parts]
        out.append("-".join(parts))
    return ",".join(out)


def _t(x) -> str:
    return str(x)[:5]  # Yext may return HH:MM or HH:MM:SS


def item_to_str(item: dict | None) -> str | None:
    """Yext day object or holidayHours item -> hours string. None when there is no item."""
    if item is None:
        return None
    if item.get("isRegularHours"):
        return REGULAR
    ivs = item.get("openIntervals") or []
    if item.get("isClosed") or not ivs:
        return CLOSED
    return ",".join(f"{_t(iv['start'])}-{_t(iv['end'])}" for iv in ivs)


def str_to_item(s: str) -> dict:
    """Hours string -> Yext holidayHours item body (without the date)."""
    s = normalise(s)
    if s == CLOSED:
        return {"isClosed": True}
    if s == REGULAR:
        return {"isRegularHours": True}
    return {"openIntervals": [{"start": a, "end": b} for a, b in (iv.split("-") for iv in s.split(","))]}


def _mins(t: str) -> int:
    return int(t[:2]) * 60 + int(t[3:])


def validate(s: str) -> list[str]:
    """Errors for an hours string, following the Yext special cases (24 h = 00:00-23:59, no overlaps)."""
    s = normalise(s)
    if s in (CLOSED, REGULAR):
        return []
    errs, spans = [], []
    for iv in s.split(","):
        parts = iv.split("-")
        if len(parts) != 2 or not all(_TIME.match(p) for p in parts):
            errs.append(f"'{iv}' is not HH:MM-HH:MM")
            continue
        a, b = parts
        if a == b:
            errs.append(f"'{iv}' starts and ends at the same time (use 00:00-23:59 for 24 h)")
            continue
        sa, sb = _mins(a), _mins(b)
        spans.append((sa, sb if sb > sa else sb + 1440))   # past midnight
    spans.sort()
    for (a1, b1), (a2, _b2) in zip(spans, spans[1:]):
        if a2 < b1:
            errs.append("intervals overlap")
    return errs


def holiday_map(hours: dict) -> dict[str, str]:
    """{date: hours string} for the entity's holidayHours."""
    return {h["date"]: item_to_str(h) for h in (hours or {}).get("holidayHours", []) if "date" in h}


def weekdays(hours: dict) -> dict[str, str | None]:
    return {d: item_to_str((hours or {}).get(d)) for d in DAYS}


def with_holiday_changes(hours: dict, changes: dict[str, str | None]) -> dict:
    """Copy of the FULL hours object with only the given dates replaced (None = remove the date).

    Weekdays, reopenDate and every other holiday date are left exactly as they were: read-modify-write.
    """
    out = copy.deepcopy(hours or {})
    keep = [h for h in out.get("holidayHours", []) if h.get("date") not in changes]
    for d, s in changes.items():
        if s is not None:
            keep.append({"date": d, **str_to_item(s)})
    out["holidayHours"] = sorted(keep, key=lambda h: h["date"])
    return out


def pretty(s: str) -> str:
    """'10:00-15:30,17:00-22:00' -> '10–15.30, 17–22' for messages."""
    def t(x):
        hh, mm = x.split(":")
        return str(int(hh)) + ("" if mm == "00" else "." + mm)
    return ", ".join("–".join(t(x) for x in iv.split("-")) for iv in normalise(s).split(","))
