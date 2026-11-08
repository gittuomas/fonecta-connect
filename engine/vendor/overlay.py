"""T6 deterministic structural overlay: terrace closure, school-break copy, follower-gated centre hours.

Importable builder. Everything comes from `ctx` and is point-in-time; no truth, no learned parameter,
no cache. Apply it to a base model's raw action frame BEFORE `harness.finalize`, so the standing guards
re-apply to the overlay's rows too:

    from exp.t6.overlay import structural_overlay
    def fit_predict(ctx):
        return structural_overlay(ctx, base(ctx), arms=("terrace", "school", "centre"))
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import engine_context as harness

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
OUT = ["entity_id", "date", "action", "hours", "confidence"]
ARMS = ("terrace", "school", "centre")
FOLLOW_MIN_RATE = 0.8
FOLLOW_MIN_N = 5


def _season_of(dates) -> np.ndarray:
    d = pd.to_datetime(pd.Series(list(dates)))
    return (d.dt.year - (d.dt.month < 7).astype(int)).to_numpy()


def _weekday_names(dates) -> np.ndarray:
    return pd.to_datetime(pd.Series(list(dates))).dt.day_of_week.map(dict(enumerate(DAYS))).to_numpy()


def _reg_table(ctx) -> pd.DataFrame:
    """Published weekday hours per entity as of ctx.snap, indexed by entity_id.

    Mirrors `harness.exact_reg`: entities.json for 2026, the regular-hours history for past seasons
    (`ctx.statics()` blanks the weekday columns outside 2026).
    """
    if ctx.season == 2026:
        return ctx.statics().set_index("entity_id")[DAYS]
    r = ctx.regular().copy()
    r["k"] = r["effective_from"].replace("before-2024", "0000-00-00")
    return r.sort_values("k").groupby("entity_id").tail(1).set_index("entity_id")[DAYS]


def _resolve(reg: pd.DataFrame, owner_ids, weekdays, values):
    """REGULAR -> the owner's published hours for that weekday; everything else unchanged."""
    out = []
    for owner, wd, v in zip(owner_ids, weekdays, values):
        if v == "REGULAR" and isinstance(owner, str) and owner in reg.index and isinstance(wd, str):
            out.append(reg.at[owner, wd])
        else:
            out.append(v)
    return out


# ------------------------------------------------------------------ the three arms
def terrace_value(ctx, rows, reg) -> pd.Series:
    """Seasonal terrace with no published winter closure and a listing shown open -> closed."""
    statics = ctx.statics().set_index("entity_id")
    hist = ctx.regular()
    published = set(hist.loc[(hist["monday"] == "closed") & (hist["saturday"] == "closed")
                             & (hist["effective_from"].str[5:] == "10-01"), "entity_id"])
    reopen = rows["entity_id"].map(statics["reopen_date"]) if "reopen_date" in statics else pd.Series(None, index=rows.index)
    fire = (rows["entity_id"].map(statics["segment"]) == "seasonal_terrace") \
        & ~rows["entity_id"].isin(published) & reopen.isna() & (rows["shown"] != "closed")
    return pd.Series(np.where(fire, "closed", None), index=rows.index, dtype=object)


def school_value(ctx, rows, reg) -> pd.Series:
    """SCHOOL_BREAK -> the business's own most recent OPEN school-break entry before its snapshot."""
    e = ctx.entries()
    e = e[(e["day_key"] == "SCHOOL_BREAK") & (e["date"] < ctx.snap) & (e["entered_at"] < ctx.snap)
          & ~e["hours"].isin(["closed", "REGULAR"])]
    last = e.sort_values("date").groupby("entity_id")["hours"].last()
    v = rows["entity_id"].map(last).where(rows["day_key"] == "SCHOOL_BREAK")
    return pd.Series(v.to_numpy(), index=rows.index, dtype=object)


def centre_value(ctx, rows, reg) -> pd.Series:
    """Follower-gated shopping-centre tenants -> last season's centre hours for the same day_key,
    only on dates where this season's centre hours are not published yet."""
    e = ctx.entries()
    centre = e[e["source"] == "API_INTEGRATION"].sort_values("entered_at")
    if centre.empty:
        return pd.Series([None] * len(rows), index=rows.index, dtype=object)
    this_date = centre.groupby(["entity_id", "date"], as_index=False).tail(1)
    this_date = this_date.rename(columns={"entity_id": "mall_id", "hours": "ch"})[["mall_id", "date", "ch"]]
    wd = _weekday_names(rows["date"])
    j = rows[["entity_id", "date", "mall_id"]].merge(this_date, on=["mall_id", "date"], how="left")
    ch = pd.Series(_resolve(reg, j["mall_id"], wd, j["ch"]), index=rows.index, dtype=object)

    last = centre.assign(cseason=_season_of(centre["date"]))
    last = last[last["cseason"] == ctx.season - 1].groupby(["entity_id", "day_key"], as_index=False).tail(1)
    last = last.rename(columns={"entity_id": "mall_id", "hours": "lc"})[["mall_id", "day_key", "lc"]]
    j2 = rows[["entity_id", "date", "day_key", "mall_id"]].merge(last, on=["mall_id", "day_key"], how="left")
    lc = pd.Series(_resolve(reg, j2["mall_id"], wd, j2["lc"]), index=rows.index, dtype=object)

    own = e[e["hours"] != "REGULAR"][["entity_id", "date", "hours"]].merge(
        rows[["entity_id", "mall_id"]].drop_duplicates(), on="entity_id", how="inner")
    own = own[own["mall_id"].notna()].merge(this_date, on=["mall_id", "date"], how="inner")
    if own.empty:
        return pd.Series([None] * len(rows), index=rows.index, dtype=object)
    score = own.assign(f=own["hours"] == own["ch"]).groupby("entity_id")["f"].agg(["mean", "size"])
    gate = (rows["entity_id"].map(score["mean"]).fillna(-1.0) >= FOLLOW_MIN_RATE) \
        & (rows["entity_id"].map(score["size"]).fillna(0) >= FOLLOW_MIN_N)
    return lc.where(gate.to_numpy() & ch.isna().to_numpy())


RULES = {"terrace": terrace_value, "school": school_value, "centre": centre_value}


# ------------------------------------------------------------------ application
def overlay_values(ctx, arms=ARMS) -> pd.DataFrame:
    """entity_id, date, ov_hours, ov_src for every predict row the overlay fires on.

    Fixed precedence terrace > school > centre; never a value equal to the shown hours.
    """
    rows = ctx.predict_rows()[["entity_id", "date", "day_key", "mall_id", "shown"]].copy()
    if rows.empty:
        return pd.DataFrame({"entity_id": [], "date": [], "ov_hours": [], "ov_src": []})
    reg = _reg_table(ctx)
    value = pd.Series([None] * len(rows), index=rows.index, dtype=object)
    source = pd.Series([None] * len(rows), index=rows.index, dtype=object)
    for arm in ARMS:                                        # fixed order, not the caller's order
        if arm not in arms:
            continue
        v = RULES[arm](ctx, rows, reg)
        take = value.isna() & v.notna()
        value[take] = v[take]
        source[take] = arm
    out = rows.assign(ov_hours=value, ov_src=source)
    out = out[out["ov_hours"].notna()].copy()
    out["ov_hours"] = out["ov_hours"].map(harness.EV.norm)
    out = out[out["ov_hours"] != out["shown"]]
    return out[["entity_id", "date", "ov_hours", "ov_src"]].reset_index(drop=True)


def structural_overlay(ctx, pred: pd.DataFrame, arms=ARMS) -> pd.DataFrame:
    """Overwrite the base's action on every row the overlay fires on. FLAG rows are preserved."""
    ov = overlay_values(ctx, arms)
    base = pred.reindex(columns=OUT).copy()
    if ov.empty:
        return base
    flags = set(map(tuple, base.loc[base["action"] == "FLAG", ["entity_id", "date"]].itertuples(index=False, name=None)))
    keys = list(zip(ov["entity_id"], ov["date"]))
    ov = ov[[k not in flags for k in keys]]
    if ov.empty:
        return base
    replaced = set(map(tuple, ov[["entity_id", "date"]].itertuples(index=False, name=None)))
    keep = base[[k not in replaced for k in zip(base["entity_id"], base["date"])]]
    add = ov.rename(columns={"ov_hours": "hours"}).assign(action="SUGGEST", confidence=np.nan)[OUT]
    return pd.concat([keep, add], ignore_index=True)
