"""Shared loaders and hours helpers for the Fonecta data analysis.

Loaded by the engine runner after configuring its public-data and cache paths.
All loaders prefer cached parquet files produced by build_cache.py. Input data
is read-only.

Conventions
- Hours strings: 'closed' | '09:00-17:00' | '10:00-14:00,17:00-22:00' | '18:00-02:00' | '00:00-23:59'
- 'REGULAR' is used for holidayHours entries with isRegularHours=True (meaning: regular weekly hours apply).
- season(d): the holiday season a date belongs to, = d.year if d.month >= 7 else d.year - 1
  (so 2025-11-08 .. 2026-01-06 is season 2025; matches the 2025 back-test window).
- SNAP_2026 = 2026-11-06, SNAP_2025 = 2025-11-06. "Before the snapshot" means timestamp < snapshot date.
"""
from __future__ import annotations

import json
import os
from datetime import date

import pandas as pd

ANALYSIS = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ["FONECTA_ENGINE_DATA"]
PKG = os.path.dirname(DATA)
CACHE = os.environ["FONECTA_ENGINE_CACHE"]
D = lambda *p: os.path.join(DATA, *p)  # noqa: E731

SNAP_2026 = "2026-11-06"
SNAP_2025 = "2025-11-06"
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# ---------------------------------------------------------------- hours helpers
def day_str(obj) -> str:
    """Yext day / holidayHours object -> hours string ('REGULAR' for isRegularHours)."""
    if not obj:
        return "closed"
    if obj.get("isRegularHours"):
        return "REGULAR"
    if obj.get("isClosed"):
        return "closed"
    ivs = obj.get("openIntervals") or []
    return ",".join(f"{i['start']}-{i['end']}" for i in ivs) or "closed"


def ivs(s: str):
    """hours string -> list of (start_min, end_min); end may exceed 1440 past midnight."""
    if s in (None, "", "closed"):
        return []
    out = []
    for iv in s.split(","):
        a, b = iv.split("-")
        sa, sb = int(a[:2]) * 60 + int(a[3:]), int(b[:2]) * 60 + int(b[3:])
        if sb <= sa and not (a == "00:00" and b == "23:59"):
            sb += 1440
        out.append((sa, sb))
    return out


def is_closed(s) -> bool:
    return s == "closed"


def same_state(a, b) -> bool:
    return (a == "closed") == (b == "closed")


def near(a, b, tol=30) -> bool:
    """Scorer's 'near' test: same state and first open / last close within tol minutes."""
    ia, ib = ivs(a), ivs(b)
    if not ia or not ib:
        return not ia and not ib
    return abs(ia[0][0] - ib[0][0]) <= tol and abs(ia[-1][1] - ib[-1][1]) <= tol


def open_minutes(s) -> int:
    return sum(b - a for a, b in ivs(s))


def season(d) -> int:
    d = pd.Timestamp(d)
    return d.year if d.month >= 7 else d.year - 1


# ---------------------------------------------------------------- loaders
def _cache(name):
    return os.path.join(CACHE, name)


def load_entities() -> pd.DataFrame:
    """One row per entity with flattened fields + weekly hours strings (mon..sun)."""
    return pd.read_parquet(_cache("entities.parquet"))


def load_entities_raw() -> dict:
    return {e["meta"]["id"]: e for e in json.load(open(D("entities.json"), encoding="utf-8"))["response"]["entities"]}


def load_holiday_history() -> pd.DataFrame:
    """All exception entries: entity_id, date, day_key, hours (string or 'REGULAR'), entered_at, source."""
    return pd.read_parquet(_cache("holiday_hours_history.parquet"))


def latest_entries(as_of: str, before_date_only: bool = False) -> pd.DataFrame:
    """Latest entry per (entity_id, date) with entered_at < as_of. If before_date_only, also entered_at < date."""
    h = load_holiday_history()
    h = h[h["entered_at"] < as_of]
    if before_date_only:
        h = h[h["entered_at"] < h["date"]]
    return h.sort_values("entered_at").groupby(["entity_id", "date"], as_index=False).tail(1)


def load_regular_history() -> pd.DataFrame:
    """entity_id, effective_from ('before-2024' or date), mon..sun hours strings."""
    return pd.read_parquet(_cache("regular_hours_history.parquet"))


def regular_hours_on(entity_id_series, date_series, as_of: str) -> pd.Series:
    """Published regular hours for each (entity, date) as known at as_of (version effective before as_of)."""
    rh = load_regular_history()
    rh = rh[(rh["effective_from"] == "before-2024") | (rh["effective_from"] < as_of)].copy()
    rh["eff_sort"] = rh["effective_from"].replace("before-2024", "0000")
    best = rh.sort_values("eff_sort").groupby("entity_id").tail(1).set_index("entity_id")
    wd = pd.to_datetime(date_series).dt.weekday.map(dict(enumerate(DAYS)))
    return pd.Series([best.at[e, w] for e, w in zip(entity_id_series, wd)], index=entity_id_series.index)


def load_reviews() -> pd.DataFrame:
    """id, locationId, rating, content, publisherDate (datetime), publisherId, n_owner_replies."""
    return pd.read_parquet(_cache("reviews.parquet"))


def load_publisher_suggestions() -> pd.DataFrame:
    """Raw suggestions table (one row per suggestion)."""
    return pd.read_parquet(_cache("publisher_suggestions.parquet"))


def load_publisher_suggestion_items() -> pd.DataFrame:
    """One row per holidayHours item inside suggestedContent: suggestion id, locationId, publisherId,
    dateCreated, status, fieldName, item_date, sugg_hours, plus the original holidayHours for that date if present."""
    return pd.read_parquet(_cache("publisher_suggestion_items.parquet"))


def load_analytics(columns=None) -> pd.DataFrame:
    return pd.read_parquet(D("analytics_daily.parquet"), columns=columns)


def load_listings() -> pd.DataFrame:
    return pd.DataFrame(json.load(open(D("listings.json"), encoding="utf-8"))["listings"])


def load_events() -> pd.DataFrame:
    return pd.read_parquet(_cache("local_events.parquet"))


def csv(name, folder="data") -> pd.DataFrame:
    if folder != "data":
        raise ValueError("Inference reads public data only")
    return pd.read_csv(D(name))






