"""C-R01-T4-3: date-specific crowd evidence about what actually happened on a past special day.

Reviews and post-day publisher edits are the only hour-resolved record of a past special day for a business
that never entered it. Measured point-in-time at the 2026 snapshot (see the kill test below):

  * holidayHours edits created 0-2 days after the day match that day's truth on 574/574 CV past special days
  * trusted review tiers give closed claims at precision 1.000 (n=365) and open claims at 1.000 (n=179)
  * the CAME_CLOSED_GOOGLE_OPEN template is only 0.643 and is dropped

This module ports `scripts/02_text_signals/signals.past_day_evidence` onto `ctx` so it is point-in-time and
deployable for test businesses. Everything inside fit_predict goes through ctx:

  ctx.reviews()   parsed with review_parser.review_events; each event resolved to the latest calendar
                  occurrence of its (country, day_key) strictly before publisherDate (lag capped at 30 days)
  ctx.edits()     restricted to item_type == holidayHours with dateCreated 0-2 days after the item date
  ctx.statics()   country, for the calendar resolution
  features.reg_for  the regular hours in force on the evidence day and on the target day

The documented SOURCE_RANK precedence picks one record per (business, day_key, season). CLOSED_EARLY keeps its
implied window ('closed already at T although online it said H' -> H.start-T). The implied past-day truth is
re-rendered onto the target date's regular hours with habitlib encode/apply, not copied as a string.

Snapshot discipline: the evidence table is built once per ctx (seen_at < ctx.snap), and the row attach then
filters it per ROW snapshot (both the evidence day and seen_at must precede the row's own snap), so a training
row from snapshot 2024-11-06 never sees evidence that did not exist yet. This is the boundary issue Codex
message 0018 found in build_anylag, handled here at attach time.

Published as lab/shared/t4_crowd (business x day_key x season: source tier, implied hours, implied state,
seen_at). No truth is used anywhere in the builder, so the table is fold-safe.

Variants (exp ids):
  t4_r01_c1   frozen CP + per-row crowd trust features only (feature value)
  t4_r01_c2   frozen CP + crowd trust features + the rendered evidence as a candidate (the hypothesis)
  t4_r01_c3   frozen CP + the rendered evidence candidate only (candidate value, separable from c1)
  t4_r01_c4   like c2 but PS_POSTDAY edits only, no review tiers (ablation of the review channel)
"""
from __future__ import annotations

import pathlib
import sys



import numpy as np
import pandas as pd

from engine_context import shared

# precedence when several sources describe the same past day (most precise first), per signals.SOURCE_RANK.
# REV_CAME (CAME_CLOSED_GOOGLE_OPEN) and PS_NOISE are dropped: measured precision 0.643 and unreliable lag.
TIERS = {"PS_POSTDAY": 0, "REV_CLOSED": 1, "REV_EARLY": 2, "REV_OPEN": 3, "REV_ARRIVED": 4}
REV_CLASS = {"CLOSED": "REV_CLOSED", "CLOSED_COMPLAINT": "REV_CLOSED", "WRONG_HOURS": "REV_CLOSED",
             "CLOSED_EARLY": "REV_EARLY", "ARRIVED_CLOSED": "REV_ARRIVED",
             "OPEN_LISTED_CLOSED": "REV_OPEN", "OPEN_THANKS": "REV_OPEN"}
MAX_REVIEW_LAG = 30          # observed lag median 0 days, p90 2 days
MAX_EDIT_LAG = 2             # the reliable post-day subset
SKIP_KEYS = ("EVENT", "SCHOOL_BREAK")
EVCOLS = ["entity_id", "day_key", "season", "date", "source", "rank", "implied_hours", "implied_state", "seen_at"]


def _nn(v):
    return not (v is None or v is pd.NaT or (isinstance(v, float) and np.isnan(v)))


def _season(dates):
    d = pd.to_datetime(dates)
    return np.where(d.dt.month >= 7, d.dt.year, d.dt.year - 1)


# ---------------------------------------------------------------- the evidence table (point-in-time, no truth)
def _review_evidence(ctx) -> pd.DataFrame:
    from review_parser import review_events

    rev = ctx.reviews()
    ev = review_events(rev)
    if not len(ev):
        return pd.DataFrame(columns=EVCOLS)
    ev = ev[ev["day_key"].notna() & ev["kind"].notna()].copy()
    ev = ev[~ev["day_key"].isin(SKIP_KEYS)]
    ev["source"] = ev["kind"].map(REV_CLASS)
    # CAME_CLOSED_GOOGLE_OPEN is the untrusted tier: 0.643 precision. Drop it, don't rank it.
    tb = ev["template"].fillna("").str.replace("+HOURS", "", regex=False)
    ev = ev[ev["source"].notna() & ~tb.str.endswith("CAME_CLOSED_GOOGLE_OPEN")]
    if not len(ev):
        return pd.DataFrame(columns=EVCOLS)

    country = ctx.statics().set_index("entity_id")["country"]
    ev["country"] = ev["locationId"].map(country)
    cal = ctx.calendar()
    occ = cal[["country", "day_key", "date"]].drop_duplicates().sort_values("date")
    byck = {k: g["date"].values for k, g in occ.groupby(["country", "day_key"])}

    def resolve(c, k, p):
        arr = byck.get((c, k))
        if arr is None:
            return None
        i = np.searchsorted(arr, p.strftime("%Y-%m-%d"), "right") - 1
        return arr[i] if i >= 0 else None

    ev["date"] = [resolve(c, k, p) for c, k, p in zip(ev["country"], ev["day_key"], ev["publisherDate"])]
    ev = ev[ev["date"].notna()].copy()
    ev["lag"] = (ev["publisherDate"] - pd.to_datetime(ev["date"])).dt.days
    ev = ev[(ev["lag"] >= 0) & (ev["lag"] <= MAX_REVIEW_LAG)]
    hours = np.where(ev["source"] == "REV_CLOSED", "closed",
                     np.where(ev["source"] == "REV_EARLY", ev["implied_truth"], None))
    state = np.where(ev["source"] == "REV_CLOSED", "closed", "open")
    return pd.DataFrame({"entity_id": ev["locationId"].values, "day_key": ev["day_key"].values,
                         "season": _season(ev["date"]), "date": ev["date"].values, "source": ev["source"].values,
                         "rank": [TIERS[s] for s in ev["source"]], "implied_hours": hours, "implied_state": state,
                         "seen_at": ev["publisherDate"].dt.strftime("%Y-%m-%d").values})


def _edit_evidence(ctx) -> pd.DataFrame:
    ed = ctx.edits()
    ed = ed[(ed["fieldName"] == "hours") & (ed["item_type"] == "holidayHours")].copy()
    ed = ed[ed["item_date"].notna() & ed["sugg_hours"].notna()]
    if not len(ed):
        return pd.DataFrame(columns=EVCOLS)
    lag = (pd.to_datetime(ed["dateCreated"]) - pd.to_datetime(ed["item_date"])).dt.days
    ed = ed[(lag >= 0) & (lag <= MAX_EDIT_LAG)].copy()
    if not len(ed):
        return pd.DataFrame(columns=EVCOLS)
    country = ctx.statics().set_index("entity_id")["country"]
    cal = ctx.calendar()
    ck = cal.drop_duplicates(["country", "date"]).set_index(["country", "date"])["day_key"]
    ed["date"] = pd.to_datetime(ed["item_date"]).dt.strftime("%Y-%m-%d")
    ed["day_key"] = [ck.get((country.get(e), d)) for e, d in zip(ed["entity_id"] if "entity_id" in ed else
                                                                 ed["locationId"], ed["date"])]
    ed = ed[ed["day_key"].notna() & ~ed["day_key"].isin(SKIP_KEYS)]
    if not len(ed):
        return pd.DataFrame(columns=EVCOLS)
    h = ed["sugg_hours"].astype(str)
    return pd.DataFrame({"entity_id": ed["locationId"].values, "day_key": ed["day_key"].values,
                         "season": _season(ed["date"]), "date": ed["date"].values, "source": "PS_POSTDAY",
                         "rank": TIERS["PS_POSTDAY"], "implied_hours": h.values,
                         "implied_state": np.where(h.values == "closed", "closed", "open"),
                         "seen_at": pd.to_datetime(ed["dateCreated"]).dt.strftime("%Y-%m-%d").values})


def build_crowd(ctx) -> pd.DataFrame:
    """One row per (entity_id, day_key, season): the highest-precedence crowd record of that past special day.

    Columns: entity_id, day_key, season, date, source, rank, implied_hours, implied_state, seen_at,
    n_ev (records in the cell), n_src (distinct tiers in the cell). Point-in-time (seen_at < ctx.snap) and
    truth-free, so it is safe across folds and deployable for test businesses.
    """
    ev = pd.concat([_edit_evidence(ctx), _review_evidence(ctx)], ignore_index=True)
    if not len(ev):
        return pd.DataFrame(columns=EVCOLS + ["n_ev", "n_src"])
    ev = ev[ev["date"] < ctx.snap].reset_index(drop=True)
    grp = ev.groupby(["entity_id", "day_key", "season"])
    counts = grp.agg(n_ev=("source", "size"), n_src=("source", "nunique")).reset_index()
    best = ev.sort_values(["rank", "seen_at"]).groupby(["entity_id", "day_key", "season"], as_index=False).head(1)
    out = best.merge(counts, on=["entity_id", "day_key", "season"], how="left")
    return out[EVCOLS + ["n_ev", "n_src"]].reset_index(drop=True)


# ---------------------------------------------------------------- rendering and the per-row attach
def _render(src_hours, src_reg, tgt_reg):
    """Encode the evidence day's outcome against its own weekday regular hours, render on the target weekday's."""
    from t1.habit_pure import classify, encode, apply as happly
    if not (_nn(src_hours) and _nn(src_reg) and _nn(tgt_reg)):
        return None
    if not isinstance(src_hours, str) or src_hours in ("", "None", "nan"):
        return None
    try:
        cls, _, _ = classify(src_hours, src_reg if _nn(src_reg) else "closed")
        return happly(encode(cls, src_hours, src_reg), tgt_reg)
    except Exception:
        return None


FEATCOLS = ["cw_has", "cw_rank", "cw_closed", "cw_has_hours", "cw_nev", "cw_nsrc", "cw_nseason", "cw_nclosed",
            "cw_nopen", "cw_back", "cw_lag", "cw_eq_shown", "cw_agree_l1", "cw_src_ps", "cw_src_revclosed",
            "cw_src_revearly", "cw_src_revopen", "cw_src_revarr"]
HIDDEN = ["cw_render", "cw_raw"]


def _attach(ctx, frame, keep_features, sources=None):
    """Add the crowd trust features and the rendered evidence to `frame`, per the row's own snapshot."""
    from features import reg_for

    tab = shared("t4_crowd", ctx, build_crowd)
    if sources is not None:
        tab = tab[tab["source"].isin(sources)]
    f = frame.reset_index(drop=True).copy()
    cells: dict = {}
    for r in tab.itertuples(index=False):
        cells.setdefault((r.entity_id, r.day_key), []).append(r)

    recs = {c: np.full(len(f), np.nan) for c in FEATCOLS}
    raw = np.array([None] * len(f), dtype=object)
    render = np.array([None] * len(f), dtype=object)
    recs["cw_has"][:] = 0.0

    l1 = f["l1_state"].values if "l1_state" in f.columns else np.full(len(f), np.nan)
    for snap, g in f.groupby("snap", sort=True):
        snap = str(snap)
        assert snap <= ctx.snap, "crowd evidence must not look past the fold snapshot"
        idx = g.index.values
        picked, ev_dates = {}, {}
        for i, e, k, s in zip(idx, g["entity_id"].values, g["day_key"].values, g["season"].values):
            opts = [r for r in cells.get((e, k), ()) if r.date < snap and r.seen_at < snap]
            if not opts:
                continue
            best = min(opts, key=lambda r: (-int(r.season), int(r.rank), r.date))
            picked[i] = (best, opts)
            ev_dates[i] = best.date
            recs["cw_has"][i] = 1.0
            recs["cw_rank"][i] = float(best.rank)
            recs["cw_closed"][i] = 1.0 if best.implied_state == "closed" else 0.0
            recs["cw_has_hours"][i] = float(_nn(best.implied_hours) and isinstance(best.implied_hours, str))
            recs["cw_nev"][i] = float(sum(int(r.n_ev) for r in opts))
            recs["cw_nsrc"][i] = float(len({r.source for r in opts}))
            recs["cw_nseason"][i] = float(len({int(r.season) for r in opts}))
            recs["cw_nclosed"][i] = float(sum(r.implied_state == "closed" for r in opts))
            recs["cw_nopen"][i] = float(sum(r.implied_state == "open" for r in opts))
            recs["cw_back"][i] = float(int(s) - int(best.season))
            recs["cw_lag"][i] = float((pd.Timestamp(best.seen_at) - pd.Timestamp(best.date)).days)
            for tag, src in (("ps", "PS_POSTDAY"), ("revclosed", "REV_CLOSED"), ("revearly", "REV_EARLY"),
                             ("revopen", "REV_OPEN"), ("revarr", "REV_ARRIVED")):
                recs[f"cw_src_{tag}"][i] = float(best.source == src)
            raw[i] = best.implied_hours
        if not picked:
            continue
        keys = np.array(sorted(picked))
        ids = pd.Series(f["entity_id"].values[keys])
        dts = pd.Series([ev_dates[i] for i in keys])
        reg_ev = reg_for(ids, dts, snap).values
        tgt_reg = f["reg"].values[keys]
        shown = f["shown"].values[keys]
        for n, i in enumerate(keys):
            h = raw[i]
            rr = _render(h, reg_ev[n], tgt_reg[n])
            render[i] = rr
            if _nn(rr) and _nn(shown[n]):
                recs["cw_eq_shown"][i] = float(rr == shown[n])
            if _nn(l1[i]):
                recs["cw_agree_l1"][i] = float((recs["cw_closed"][i] == 1.0) == (float(l1[i]) == 2.0))

    for c in FEATCOLS:
        f[c] = recs[c]
    f["lab_cw_render"] = render
    f["lab_cw_raw"] = raw
    if not keep_features:
        f = f.drop(columns=FEATCOLS)
    return f


# ---------------------------------------------------------------- candidates
def _new_cand_frame(c, f, pairs, flag):
    """Candidate rows for (rid, cand) pairs absent from c, with make_candidates' own feature tail."""
    from features import hstat
    if not len(pairs):
        return c
    have = set(zip(c["rid"].tolist(), c["cand"].tolist()))
    pairs = pairs.drop_duplicates(["rid", "cand"]).reset_index(drop=True)
    seen = np.array([(r, v) in have for r, v in zip(pairs["rid"], pairs["cand"])])
    hit = {(r, v) for r, v in zip(pairs.loc[seen, "rid"], pairs.loc[seen, "cand"])}
    m = np.array([(r, v) in hit for r, v in zip(c["rid"], c["cand"])])
    if m.any():
        c.loc[m, flag] = 1.0
        c.loc[m, "n_sources"] = c.loc[m, "n_sources"] + 1
    fresh = pairs[~seen]
    if not len(fresh):
        return c
    n = pd.DataFrame({"rid": fresh["rid"].values, "cand": fresh["cand"].values})
    for col in c.columns:
        if col in ("rid", "cand"):
            continue
        n[col] = 0.0
    n[flag] = 1.0
    n["n_sources"] = 1.0
    st = np.array([hstat(x) for x in n["cand"].tolist()], dtype=float)
    n["cand_closed"], n["cand_open"], n["cand_close"], n["cand_min"], n["cand_niv"] = st.T
    ro = f["reg_open"].values[n["rid"].values]
    rc = f["reg_close"].values[n["rid"].values]
    rm = f["reg_min"].values[n["rid"].values]
    n["cand_open_delta"] = n["cand_open"] - ro
    n["cand_close_delta"] = n["cand_close"] - rc
    n["cand_min_ratio"] = n["cand_min"] / np.where(rm > 0, rm, np.nan)
    sc = f["shown_closed"].values[n["rid"].values]
    n["cand_state_flip"] = (n["cand_closed"] != sc).astype(float)
    return pd.concat([c, n[c.columns]], ignore_index=True)


def extra_cands_crowd(c, f):
    v = pd.Series(f["lab_cw_render"].values, dtype=object)
    shown = pd.Series(f["shown"].values, dtype=object)
    ok = np.array([_nn(x) and str(x) not in ("None", "nan") and x != s for x, s in zip(v, shown)])
    c = c.copy()
    c["from_crowd"] = 0.0
    return _new_cand_frame(c, f, pd.DataFrame({"rid": np.arange(len(f))[ok], "cand": v[ok].values}), "from_crowd")
