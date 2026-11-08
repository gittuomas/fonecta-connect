"""T6 reusable: review-detected current weekly hours (REGULAR_CHANGED specs), point-in-time from ctx.

Detector: `ctx.reviews()` (already filtered to publisherDate < ctx.snap) parsed with
`scripts/02_text_signals/review_parser.parse_review`. A review whose flags contain
REGULAR_CHANGED and whose `regular_week` parses states the business's current weekly
hours. Latest such review per business wins, per weekday.

No truth, no labels, no fold-specific state: the table is a pure function of
(`ctx.snap`), so it is identical across folds and deployable for test businesses.
"""
import os
import sys

import pandas as pd

import review_parser as RP  # noqa: E402

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
SPEC_COLS = ["entity_id", "weekday", "hours", "pdate"]


def regular_week_specs(ctx) -> pd.DataFrame:
    """entity_id, weekday (0=Monday), hours, pdate. Latest review per business wins."""
    r = ctx.reviews()
    r = r[r["content"].notna()].sort_values("publisherDate")
    rows = []
    for eid, txt, pdte in zip(r["locationId"], r["content"], r["publisherDate"]):
        if not isinstance(txt, str):
            continue
        parsed = RP.parse_review(txt)
        if "REGULAR_CHANGED" not in (parsed.get("flags") or []):
            continue
        week = parsed.get("regular_week")
        if not isinstance(week, dict):
            continue
        for i, d in enumerate(DAYS):
            h = week.get(d)
            if isinstance(h, str) and h:
                rows.append((eid, i, h, str(pdte)[:10]))
    out = pd.DataFrame(rows, columns=SPEC_COLS)
    if out.empty:
        return out
    return out.drop_duplicates(["entity_id", "weekday"], keep="last").reset_index(drop=True)


def spec_rows(ctx):
    """Predict rows of a spec-carrying business joined to the parsed hours for that weekday.

    Returns entity_id, date, shown, hours (the parsed weekly hours). Rows whose parsed
    hours equal the shown hours are dropped: suggesting them is never useful.
    """
    spec = regular_week_specs(ctx)
    p = ctx.predict_rows()[["entity_id", "date", "shown"]]
    if spec.empty:
        return p.iloc[0:0].assign(hours=pd.Series(dtype="object"))
    p = p[p["entity_id"].isin(set(spec["entity_id"]))].copy()
    if p.empty:
        return p.assign(hours=pd.Series(dtype="object"))
    p["weekday"] = pd.to_datetime(p["date"]).dt.dayofweek
    p = p.merge(spec[["entity_id", "weekday", "hours"]], on=["entity_id", "weekday"], how="inner")
    p = p[p["hours"].notna() & (p["hours"] != p["shown"])]
    return p[["entity_id", "date", "shown", "hours"]].reset_index(drop=True)


def spec_fill(ctx, base_raw, gate="abstain"):
    """Overlay the parsed weekly hours on top of a base model's RAW output (before finalize).

    gate="abstain": only on rows the base leaves at NO_ACTION after the standing guards.
    gate="all":     on every row of a spec-carrying business, overriding the base's own
                    suggestion. FLAG rows are never overridden in either mode.
    """
    import engine_context as harness

    assert gate in ("abstain", "all")
    cand = spec_rows(ctx)
    if cand.empty:
        return base_raw
    final = harness.finalize(base_raw, ctx)
    if gate == "abstain":
        block = set(zip(final["entity_id"], final["date"]))
    else:
        f = final[final["action"] == "FLAG"]
        block = set(zip(f["entity_id"], f["date"]))
    cand = cand[[(e, d) not in block for e, d in zip(cand["entity_id"], cand["date"])]]
    if cand.empty:
        return base_raw
    fill = pd.DataFrame({"entity_id": cand["entity_id"].to_numpy(),
                         "date": cand["date"].to_numpy(),
                         "action": "SUGGEST",
                         "hours": cand["hours"].to_numpy(),
                         "confidence": float("nan")})
    keys = set(zip(fill["entity_id"], fill["date"]))
    keep = base_raw[[(e, d) not in keys for e, d in zip(base_raw["entity_id"], base_raw["date"])]]
    return pd.concat([keep, fill[list(base_raw.columns)] if set(base_raw.columns) == set(fill.columns) else fill],
                     ignore_index=True)
