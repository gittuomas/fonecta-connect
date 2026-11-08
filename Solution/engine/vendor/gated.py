"""T6 critic: the structural overlay restricted to rows the base leaves at NO_ACTION.

k07 shows the stack's override component (133 of 143 centre rows and 12 of 34 school rows in
dev 2026) is a low-mean, high-variance bet: in the 2025 holdout it destroys 15.2 payoff points of
already-exact base answers and recovers 19.6 elsewhere. This variant keeps only the abstention
fill, which is where every terrace point and most school and centre points come from, and which
is also the gate C-R02-T6-2's review fill already uses. overlay.py is NOT modified.
"""
import pandas as pd

import engine_context as harness
from overlay import OUT, ARMS, overlay_values


def abstain_overlay(ctx, pred, arms=ARMS):
    ov = overlay_values(ctx, arms)
    base = pred.reindex(columns=OUT).copy()
    if ov.empty:
        return base
    taken = set(zip(*map(list, [harness.finalize(pred, ctx)["entity_id"], harness.finalize(pred, ctx)["date"]])))
    ov = ov[[k not in taken for k in zip(ov["entity_id"], ov["date"])]]
    if ov.empty:
        return base
    replaced = set(zip(ov["entity_id"], ov["date"]))
    keep = base[[k not in replaced for k in zip(base["entity_id"], base["date"])]]
    add = ov.rename(columns={"ov_hours": "hours"}).assign(action="SUGGEST", confidence=float("nan"))[OUT]
    return pd.concat([keep, add], ignore_index=True)
