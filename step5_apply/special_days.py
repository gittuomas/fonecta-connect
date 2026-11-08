"""day_key lookup (XMAS_EVE, NYE, EVENT, …) so callers don't have to send it.

Order: the challenge's target rows for that business and date (they include EVENT and SCHOOL_BREAK days), then the
country's special-day calendar. Unknown -> "".
"""
from __future__ import annotations

import csv
from functools import lru_cache

from .config import DATA, MATERIALS

COUNTRIES = ("FI", "SE", "NO")
NOT_YEARLY = {"EVENT", ""}   # a one-off event night is not something to remember "every year"


@lru_cache(maxsize=1)
def _calendar() -> dict[tuple[str, str], str]:
    with open(DATA / "calendar_special_days.csv", encoding="utf-8") as f:
        return {(r["country"], r["date"]): r["day_key"] for r in csv.DictReader(f)}


@lru_cache(maxsize=1)
def _targets() -> dict[tuple[str, str], str]:
    out = {}
    for name in ("targets_2026.csv", "targets_2025_backtest.csv"):
        path = MATERIALS / "evaluation" / name
        if path.exists():
            with open(path, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    out[(r["entity_id"], r["date"])] = r["day_key"]
    return out


def country_of(entity_id: str, fallback: str | None = None) -> str | None:
    p = entity_id[:2].upper()
    return p if p in COUNTRIES else fallback


def day_key(entity_id: str, date: str, country: str | None = None) -> str:
    k = _targets().get((entity_id, date))
    if k:
        return k
    c = country or country_of(entity_id)
    if c:
        return _calendar().get((c, date), "")
    keys = {_calendar().get((x, date)) for x in COUNTRIES} - {None}
    return keys.pop() if len(keys) == 1 else ""
