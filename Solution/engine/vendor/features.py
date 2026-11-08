"""Point-in-time feature builder for the ML probe (agent 06).

    rows  = DataFrame[entity_id, date, day_key]  (+ optional day_type, event_id)
    feats = build_features(rows, snap)            # snap e.g. '2025-11-06'

Honesty rules (all enforced here, nothing else is read):
- holiday entries: entered_at < snap (the latest such entry per (entity, date))
- regular hours: version with effective_from < snap (or 'before-2024')
- reviews: publisherDate < snap; publisher suggestions: dateCreated < snap (status / dateResolved never used)
- analytics: date < snap
- entities.json is only used for static fields (country, segment, city, mall link, customer_since);
  its holidayHours / closed flag (2026 state) are NOT used.
- peer_benchmarks.csv is an aggregate over seasons 2023-2025 (no season column), so for 2024/2025 snapshots
  it carries some future information. Flagged as a leakage risk; measured by ablation in train_state.py.

Row universes:
- rows_targets(2026)       targets_2026.csv (all splits)
- rows_targets(2025)       targets_2025_backtest.csv (dev)
- rows_truth(2024)         dev_truth_history rows in the 2024 window (dev only), same day_keys as targets
- rows_selfsup(Y)          all dev+test businesses, window of season Y, (entity, date) that have an entry
                           made before the day; label = that entry (truth proxy, ~99% accurate on dev)
"""
from __future__ import annotations

import os
import re
import sys
from functools import lru_cache

import numpy as np
import pandas as pd

from common import (DAYS, csv, ivs, load_entities, load_holiday_history, load_publisher_suggestion_items,  # noqa: E402
                    load_regular_history, load_reviews, load_publisher_suggestions, load_analytics)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")


SNAPS = {2024: "2024-11-06", 2025: "2025-11-06", 2026: "2026-11-06"}
TARGET_KEYS = ["BLACK_FRIDAY", "BOXING_DAY", "DEC23", "EPIPHANY", "EPIPHANY_EVE", "EVENT", "FATHERS_DAY",
               "INDEPENDENCE_DAY", "INTERDAY", "LUCIA", "NEW_YEARS_DAY", "NYE", "SCHOOL_BREAK", "XMAS_DAY", "XMAS_EVE"]
CROSS_KEYS = ["XMAS_EVE", "XMAS_DAY", "BOXING_DAY", "NYE", "NEW_YEARS_DAY", "EPIPHANY", "DEC23", "INDEPENDENCE_DAY"]
SPRING_KEYS = ["GOOD_FRIDAY", "EASTER_MONDAY", "MAY_DAY", "MIDSUMMER_EVE", "ASCENSION", "ALL_SAINTS"]
CAT_COLS = ["day_key", "day_type", "country", "segment", "city_tier"]
PERM_WORDS = ("lopettanut", "pysyvästi", "stängt för gott", "permanent", "lagt ned", "stengt for godt", "closed down")
# templated complaint reviews (see analysis lead L12): closed door / wrong hours, 4 languages
COMPLAINT_RE = re.compile(
    r"ovi oli kiinni|olivat jo kiinni|oli suljettu|suljetun oven|eri ajat|väärin|vanhat tiedot|netissä luki|"
    r"var stängt|det var stängt|stängt på|stängde|fel tider|trots att google|"
    r"var stengt|det var stengt|stengt |stengte|feil|selv om det sto|"
    r"was closed|it was closed|closed already|google said|wrong|online it said", re.I)
OPEN_WHEN_LISTED_CLOSED_RE = re.compile(r"actually open|olittekin auki|olitte auki vaikka|hade öppet trots|hadde åpent selv", re.I)


def wseason(d: pd.Series) -> pd.Series:
    d = pd.to_datetime(d)
    return np.where(d.dt.month >= 7, d.dt.year, d.dt.year - 1)


# ----------------------------------------------------------------- hours parsing (cached per unique string)
@lru_cache(maxsize=None)
def hstat(s):
    """-> (closed, open_start_min, close_end_min, open_minutes, n_intervals) ; NaN for missing."""
    if s is None or (isinstance(s, float) and np.isnan(s)) or s == "REGULAR":
        return (np.nan,) * 5
    iv = ivs(s)
    if not iv:
        return (1.0, np.nan, np.nan, 0.0, 0.0)
    return (0.0, float(iv[0][0]), float(iv[-1][1]), float(sum(b - a for a, b in iv)), float(len(iv)))


def hstats_df(series: pd.Series, prefix: str) -> pd.DataFrame:
    arr = np.array([hstat(x) for x in series.tolist()], dtype=float) if len(series) else np.zeros((0, 5))
    return pd.DataFrame(arr, index=series.index, columns=[f"{prefix}_{c}" for c in ("closed", "open", "close", "min", "niv")])


def state_code(h, reg):
    """0 = equals regular (incl. REGULAR entries), 1 = open special, 2 = closed, NaN = no entry."""
    if h is None or (isinstance(h, float) and np.isnan(h)):
        return np.nan
    if h == "REGULAR" or h == reg:
        return 0.0
    if h == "closed":
        return 2.0
    return 1.0


# ----------------------------------------------------------------- static tables
@lru_cache(maxsize=None)
def static():
    ent = load_entities()[["entity_id", "country", "segment", "city", "mall_id", "customer_since", "entity_role"]]
    cities = csv("cities.csv")[["city", "city_tier"]]
    ent = ent.merge(cities, on="city", how="left")
    cal = csv("calendar_special_days.csv")[["country", "date", "day_key", "day_type"]].copy()
    cal["season"] = wseason(cal["date"])
    cal = cal.sort_values("date")
    cal["rank"] = cal.groupby(["country", "day_key", "season"]).cumcount()
    hol = load_holiday_history()
    reg = load_regular_history()
    peer = csv("peer_benchmarks.csv")
    splits = csv("splits.csv")
    return ent, cal, hol, reg, peer, splits


@lru_cache(maxsize=None)
def reg_at(snap: str) -> pd.DataFrame:
    """Published regular hours (mon..sun strings) per entity as known at snap, plus change metadata."""
    _, _, _, reg, _, _ = static()
    r = reg[(reg["effective_from"] == "before-2024") | (reg["effective_from"] < snap)].copy()
    r["eff_sort"] = r["effective_from"].replace("before-2024", "0000-00-00")
    r = r.sort_values("eff_sort")
    best = r.groupby("entity_id").tail(1).set_index("entity_id")[DAYS]
    meta = r.groupby("entity_id").agg(reg_n_versions=("eff_sort", "size"), reg_last_change=("eff_sort", "max"))
    last = pd.to_datetime(meta["reg_last_change"].replace("0000-00-00", "2023-01-01"))
    meta["reg_days_since_change"] = (pd.Timestamp(snap) - last).dt.days
    meta["reg_all_closed"] = (best == "closed").all(axis=1).astype(float)
    return best.join(meta[["reg_n_versions", "reg_days_since_change", "reg_all_closed"]])


def reg_for(entity_ids: pd.Series, dates: pd.Series, snap: str) -> pd.Series:
    """Regular-hours string for each (entity, date weekday) at snap."""
    ra = reg_at(snap)
    wd = pd.to_datetime(dates).dt.weekday.values
    idx = ra.index.get_indexer(entity_ids.values)
    vals = ra[DAYS].values
    out = np.array([vals[i, w] if i >= 0 else None for i, w in zip(idx, wd)], dtype=object)
    return pd.Series(out, index=entity_ids.index)


@lru_cache(maxsize=None)
def entries_at(snap: str) -> pd.DataFrame:
    """Latest entry per (entity, date) with entered_at < snap; hours resolved (REGULAR -> regular at snap)."""
    _, _, hol, _, _, _ = static()
    h = hol[hol["entered_at"] < snap].sort_values("entered_at").groupby(["entity_id", "date"], as_index=False).tail(1)
    h = h.reset_index(drop=True)
    h["is_regular_entry"] = (h["hours"] == "REGULAR").astype(float)
    regs = reg_for(h["entity_id"], h["date"], snap)
    h["reg_then"] = regs.values
    h["res"] = np.where(h["hours"] == "REGULAR", h["reg_then"], h["hours"])
    h["before_day"] = (h["entered_at"] < h["date"]).astype(float)
    h["lead_days"] = (pd.to_datetime(h["date"]) - pd.to_datetime(h["entered_at"].str[:10])).dt.days
    h["state"] = [state_code(a, b) for a, b in zip(h["hours"], h["reg_then"])]
    return h


# ----------------------------------------------------------------- signals
@lru_cache(maxsize=None)
def reviews_tbl():
    r = load_reviews()[["locationId", "content", "publisherDate", "rating"]].copy()
    c = r["content"].fillna("")
    r["complaint"] = c.str.contains(COMPLAINT_RE).astype(float)
    r["open_complaint"] = c.str.contains(OPEN_WHEN_LISTED_CLOSED_RE).astype(float)
    r["perm"] = c.str.lower().apply(lambda s: any(w in s for w in PERM_WORDS)).astype(float)
    r["day"] = r["publisherDate"].dt.strftime("%Y-%m-%d")
    return r.drop(columns=["content"])


def review_feats(snap: str) -> pd.DataFrame:
    r = reviews_tbl()
    s = pd.Timestamp(snap)
    a = r[(r["day"] < snap) & (r["publisherDate"] >= s - pd.Timedelta(days=365))]
    g = a.groupby("locationId").agg(rev_n365=("rating", "size"), rev_mean_rating=("rating", "mean"),
                                    rev_complaint365=("complaint", "sum"), rev_open_complaint365=("open_complaint", "sum"))
    b = r[(r["day"] < snap) & (r["publisherDate"] >= s - pd.Timedelta(days=150))]
    g2 = b.groupby("locationId").agg(rev_perm150=("perm", "sum"))
    return g.join(g2, how="outer")


def ps_feats(snap: str):
    psi = load_publisher_suggestion_items()
    p = psi[psi["dateCreated"] < snap].copy()
    s = pd.Timestamp(snap)
    recent = p[pd.to_datetime(p["dateCreated"]) >= s - pd.Timedelta(days=365)]
    g = recent.groupby("locationId").agg(ps_n365=("id", "size")).join(
        p[p["fieldName"] == "closed"].groupby("locationId").size().rename("ps_closed_field_ever"), how="outer")
    items = p[p["item_type"] == "holidayHours"].sort_values("dateCreated").groupby(["locationId", "item_date"]).tail(1)
    items = items[["locationId", "item_date", "sugg_hours"]].rename(columns={"locationId": "entity_id", "item_date": "date"})
    return g, items


@lru_cache(maxsize=None)
def analytics_feats(snap: str) -> pd.DataFrame:
    s = pd.Timestamp(snap)
    a = load_analytics(columns=["date", "entity_id", "GOOGLE_SEARCH_VIEWS", "GOOGLE_PHONE_CALLS"])
    a["date"] = pd.to_datetime(a["date"])
    a = a[(a["date"] < s) & (a["date"] >= s - pd.Timedelta(days=60))]
    g = a.groupby("entity_id").agg(an_views60=("GOOGLE_SEARCH_VIEWS", "mean"), an_calls60=("GOOGLE_PHONE_CALLS", "mean"))
    g["an_call_rate"] = g["an_calls60"] / (g["an_views60"] + 1)
    return g


# ----------------------------------------------------------------- row universes






# ----------------------------------------------------------------- the feature builder
def build_features(rows: pd.DataFrame, snap: str) -> pd.DataFrame:
    ent, cal, hol, reg, peer, splits = static()
    S = pd.Timestamp(snap)
    f = rows[["entity_id", "date", "day_key"]].copy().reset_index(drop=True)
    f = f.merge(ent[["entity_id", "country", "segment", "city_tier", "mall_id", "customer_since"]], on="entity_id", how="left")
    dt = pd.to_datetime(f["date"])
    f["weekday"] = dt.dt.weekday
    f["season"] = wseason(f["date"])
    f["days_ahead"] = (dt - S).dt.days
    f["cust_age_days"] = (S - pd.to_datetime(f["customer_since"])).dt.days
    f["in_mall"] = f["mall_id"].notna().astype(float)
    # day_type from the calendar (EVENT / SCHOOL_BREAK rows are their own type)
    dtmap = dict(zip(zip(cal["country"], cal["date"], cal["day_key"]), cal["day_type"]))
    f["day_type"] = [dtmap.get((c, d, k), k) for c, d, k in zip(f["country"], f["date"], f["day_key"])]

    # regular hours at snap for the target weekday
    f["reg"] = reg_for(f["entity_id"], f["date"], snap).values
    f = pd.concat([f, hstats_df(f["reg"], "reg")], axis=1)
    ra = reg_at(snap)[["reg_n_versions", "reg_days_since_change", "reg_all_closed"]]
    f = f.merge(ra, left_on="entity_id", right_index=True, how="left")

    # entry for the date already made before snap, and what is shown at snap
    ea = entries_at(snap)
    now = ea[["entity_id", "date", "hours", "res"]].rename(columns={"hours": "now_raw", "res": "now_res"})
    f = f.merge(now, on=["entity_id", "date"], how="left")
    f["has_entry_now"] = f["now_raw"].notna().astype(float)
    f["shown"] = np.where(f["now_res"].notna(), f["now_res"], f["reg"])
    f = pd.concat([f, hstats_df(f["shown"], "shown")], axis=1)
    f["shown_eq_reg"] = (f["shown"] == f["reg"]).astype(float)

    # same day_key one / two seasons back. Multi-date keys (INTERDAY): the same-rank date if the business has an
    # entry there, else the nearest-rank date that has one (like the baseline, which takes any INTERDAY entry).
    # EVENT / SCHOOL_BREAK rows are not matched.
    calk = dict(zip(zip(cal["country"], cal["date"], cal["day_key"]), cal["rank"]))
    f["rank"] = [calk.get((c, d, k), 0) for c, d, k in zip(f["country"], f["date"], f["day_key"])]
    calr = dict(zip(zip(cal["country"], cal["day_key"], cal["season"], cal["rank"]), cal["date"]))
    ekeys = set(zip(ea["entity_id"], ea["date"]))
    for lag, pre in ((1, "l1"), (2, "l2")):
        pdates, same_rank = [], []
        for e_, c, k, s, r in zip(f["entity_id"], f["country"], f["day_key"], f["season"], f["rank"]):
            if k in ("EVENT", "SCHOOL_BREAK"):
                pdates.append(None)
                same_rank.append(np.nan)
                continue
            opts = [calr.get((c, k, s - lag, rr)) for rr in sorted(range(4), key=lambda x: (abs(x - r), x))]
            opts = [o for o in opts if o]
            pick = next((o for o in opts if (e_, o) in ekeys), opts[0] if opts else None)
            pdates.append(pick)
            same_rank.append(float(bool(opts) and pick == opts[0]))
        f[f"{pre}_date"] = pdates
        f[f"{pre}_same_rank"] = same_rank
        e = ea[["entity_id", "date", "hours", "res", "before_day", "state"]].rename(
            columns={"date": f"{pre}_date", "hours": f"{pre}_raw", "res": f"{pre}_res_then", "before_day": f"{pre}_before_day",
                     "state": f"{pre}_state"})
        f = f.merge(e, on=["entity_id", f"{pre}_date"], how="left")
        f[f"{pre}_has"] = f[f"{pre}_raw"].notna().astype(float)
        # candidate = the entry, REGULAR resolved with today's regular hours for the target weekday
        f[f"{pre}_cand"] = np.where(f[f"{pre}_raw"] == "REGULAR", f["reg"], f[f"{pre}_raw"])
        st = hstats_df(f[f"{pre}_cand"], pre)
        f = pd.concat([f, st], axis=1)
        f[f"{pre}_open_delta"] = f[f"{pre}_open"] - f["reg_open"]
        f[f"{pre}_close_delta"] = f[f"{pre}_close"] - f["reg_close"]
        f[f"{pre}_min_ratio"] = f[f"{pre}_min"] / f["reg_min"].replace(0, np.nan)
        f[f"{pre}_eq_shown"] = np.where(f[f"{pre}_has"] == 1, (f[f"{pre}_cand"] == f["shown"]).astype(float), np.nan)
        f = f.drop(columns=[f"{pre}_res_then"])
    f["l1_eq_l2"] = np.where((f["l1_has"] == 1) & (f["l2_has"] == 1), (f["l1_cand"] == f["l2_cand"]).astype(float), np.nan)

    # business-level history: last season window and last 365 days
    ea2 = ea.copy()
    ea2["season"] = wseason(ea2["date"])
    md = ea2["date"].str[5:]
    in_win = (md >= "11-07") | (md <= "01-06")
    last_win = ea2[in_win & ea2["day_key"].isin(TARGET_KEYS)]
    for lag, pre in ((1, "w1"), (2, "w2")):
        sub = last_win[last_win["season"] == (pd.Timestamp(snap).year - lag)]
        g = sub.groupby("entity_id").agg(**{f"{pre}_n": ("state", "size"),
                                            f"{pre}_closed_share": ("state", lambda x: (x == 2).mean()),
                                            f"{pre}_special_share": ("state", lambda x: (x == 1).mean()),
                                            f"{pre}_reg_share": ("state", lambda x: (x == 0).mean())})
        f = f.merge(g, left_on="entity_id", right_index=True, how="left")
        f[f"{pre}_n"] = f[f"{pre}_n"].fillna(0)
    yr = ea2[(pd.to_datetime(ea2["date"]) >= S - pd.Timedelta(days=365)) & (ea2["date"] < snap)]
    g = yr.groupby("entity_id").agg(y_n=("state", "size"), y_closed_share=("state", lambda x: (x == 2).mean()),
                                    y_special_share=("state", lambda x: (x == 1).mean()),
                                    y_before_day=("before_day", "sum"), y_lead_med=("lead_days", "median"))
    # diligence: share of the country's special days in the last 365 days with an entry made before the day
    ncal = cal[(pd.to_datetime(cal["date"]) >= S - pd.Timedelta(days=365)) & (cal["date"] < snap)].groupby("country").size()
    f = f.merge(g, left_on="entity_id", right_index=True, how="left")
    f["y_n"] = f["y_n"].fillna(0)
    f["y_before_day"] = f["y_before_day"].fillna(0)
    f["diligence"] = f["y_before_day"] / f["country"].map(ncal)
    allh = ea2.groupby("entity_id").agg(hist_n=("state", "size"))
    f = f.merge(allh, left_on="entity_id", right_index=True, how="left")
    f["hist_n"] = f["hist_n"].fillna(0)

    # cross-holiday states: last-season window keys and the spring/autumn keys of the last 365 days
    sk = dict(zip(zip(ea2["entity_id"], ea2["date"]), ea2["state"]))
    for k in CROSS_KEYS:
        d1 = cal[(cal["day_key"] == k) & (cal["season"] == S.year - 1) & (cal["rank"] == 0)].set_index("country")["date"]
        keys = list(zip(f["entity_id"], f["country"].map(d1)))
        f[f"x_{k}"] = [sk.get(kk, np.nan) if isinstance(kk[1], str) else np.nan for kk in keys]
    for k in SPRING_KEYS:
        c2 = cal[(cal["day_key"] == k) & (cal["date"] < snap) & (pd.to_datetime(cal["date"]) >= S - pd.Timedelta(days=365))]
        d1 = c2.sort_values("date").groupby("country").tail(1).set_index("country")["date"]
        keys = list(zip(f["entity_id"], f["country"].map(d1)))
        f[f"x_{k}"] = [sk.get(kk, np.nan) if isinstance(kk[1], str) else np.nan for kk in keys]

    # shopping centre: its entry for the date (before snap), and whether the business followed it last season
    m = ea[["entity_id", "date", "hours", "res"]].rename(columns={"entity_id": "mall_id", "hours": "mall_raw", "res": "mall_res"})
    f = f.merge(m, on=["mall_id", "date"], how="left")
    f["mall_has"] = f["mall_raw"].notna().astype(float)
    f["mall_cand"] = np.where(f["mall_raw"] == "REGULAR", np.nan, f["mall_raw"])  # centre REGULAR = no info about tenant
    f = pd.concat([f, hstats_df(f["mall_cand"], "mall")], axis=1)
    f["mall_eq_shown"] = np.where(f["mall_cand"].notna(), (f["mall_cand"] == f["shown"]).astype(float), np.nan)
    ml1 = ea[["entity_id", "date", "res"]].rename(columns={"entity_id": "mall_id", "date": "l1_date", "res": "mall_l1"})
    f = f.merge(ml1, on=["mall_id", "l1_date"], how="left")
    f["mall_l1_follow"] = np.where(f["mall_l1"].notna() & (f["l1_has"] == 1), (f["mall_l1"] == f["l1_cand"]).astype(float), np.nan)
    f = f.drop(columns=["mall_l1"])

    # peers (aggregate 2023-2025; leakage risk for back-tests, see docstring)
    pcols = ["n_entity_days", "share_no_exception_entered", "share_entered_closed", "share_entered_special_hours",
             "share_entered_regular", "closed_share_among_entered"]
    pr = peer.rename(columns={c: f"peer_{c}" for c in pcols})
    pr["peer_cand"] = np.where(pr["median_open_special"].notna() & pr["median_close_special"].notna(),
                               pr["median_open_special"].astype(str) + "-" + pr["median_close_special"].astype(str), None)
    f = f.merge(pr[["country", "city_tier", "segment", "day_key", "peer_cand"] + [f"peer_{c}" for c in pcols]],
                on=["country", "city_tier", "segment", "day_key"], how="left")
    ps = hstats_df(f["peer_cand"], "peer")[["peer_open", "peer_close", "peer_min"]]
    f = pd.concat([f, ps], axis=1)
    f["peer_open_delta"] = f["peer_open"] - f["reg_open"]
    f["peer_close_delta"] = f["peer_close"] - f["reg_close"]

    # reviews, publisher suggestions, analytics
    f = f.merge(review_feats(snap), left_on="entity_id", right_index=True, how="left")
    for c in ("rev_n365", "rev_complaint365", "rev_open_complaint365", "rev_perm150"):
        f[c] = f[c].fillna(0)
    psg, items = ps_feats(snap)
    f = f.merge(psg, left_on="entity_id", right_index=True, how="left")
    f["ps_n365"] = f["ps_n365"].fillna(0)
    f["ps_closed_field_ever"] = f["ps_closed_field_ever"].fillna(0)
    f = f.merge(items.rename(columns={"sugg_hours": "ps_cand"}), on=["entity_id", "date"], how="left")
    f["ps_has"] = f["ps_cand"].notna().astype(float)
    f["ps_closed"] = np.where(f["ps_has"] == 1, (f["ps_cand"] == "closed").astype(float), np.nan)
    it1 = items.rename(columns={"date": "l1_date", "sugg_hours": "ps_l1"})
    f = f.merge(it1, on=["entity_id", "l1_date"], how="left")
    f["ps_l1_has"] = f["ps_l1"].notna().astype(float)
    f["ps_l1_closed"] = np.where(f["ps_l1_has"] == 1, (f["ps_l1"] == "closed").astype(float), np.nan)
    f["ps_l1_eq_l1"] = np.where((f["ps_l1_has"] == 1) & (f["l1_has"] == 1), (f["ps_l1"] == f["l1_cand"]).astype(float), np.nan)
    f = f.merge(analytics_feats(snap), left_on="entity_id", right_index=True, how="left")

    f["snap"] = snap
    for c in CAT_COLS:
        f[c] = f[c].astype(str)
    assert len(f) == len(rows), (len(f), len(rows))
    return f


# string-valued columns kept for candidates / bookkeeping, not model inputs
NON_FEATURES = {"entity_id", "date", "mall_id", "customer_since", "reg", "now_raw", "now_res", "shown", "rank",
                "l1_date", "l2_date", "l1_raw", "l2_raw", "l1_cand", "l2_cand", "mall_raw", "mall_res", "mall_cand",
                "peer_cand", "ps_cand", "ps_l1", "snap", "season", "split", "truth_hours"}


LABEL_COLS = {"displayed_hours_at_snapshot", "expected_action", "mismatch_type", "expected_search_views"}


def feature_columns(df: pd.DataFrame):
    return [c for c in df.columns if c not in NON_FEATURES and c not in LABEL_COLS and not c.startswith("lab_")]




