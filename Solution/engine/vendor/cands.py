"""Candidate generation + candidate-level features/labels for the candidate scorer (model c).

Each target row gets candidate hours strings from these sources (all point-in-time, from the feature table):
  l1     same day_key last season (REGULAR entries resolved to today's regular hours)
  l2     same day_key two seasons back
  mall   the shopping centre's entry for the date (entered before snap, non-REGULAR)
  peer   peer median special hours (country x city tier x segment x day_key)
  ps     publisher (Google/Apple/...) user edit for the date, created before snap
  psl1   publisher user edit for last season's same day (evidence of last season's truth)
  hmode  the business's most frequent open special-hours string in all entries before snap (cross-holiday habit)
  closed always proposed
  reg    today's regular hours, only when an entry made before snap means shown != regular
Candidates equal to what is shown are dropped (suggesting them can only score -0.1 or worse).

Outcome classes per candidate c (c != shown), with the scorer's payoffs for SUGGEST c:
  0 EXACT  truth == c                                   +1
  1 NEAR   truth != shown, same state, near(c, truth)    +0.6
  2 FAR    truth != shown, same state, not near          +0.2
  3 WRONG  truth != shown, other state than c            -1
  4 SHOWN  truth == shown (no change needed)            -0.5 if c has shown's state, else -1
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from features import entries_at, hstat  # noqa: E402
from common import near  # noqa: E402

SOURCES = ["l1", "l2", "mall", "peer", "ps", "psl1", "hmode", "closed", "reg"]


def hist_mode(snap: str) -> pd.Series:
    ea = entries_at(snap)
    sp = ea[ea["state"] == 1]
    return sp.groupby("entity_id")["res"].agg(lambda x: x.value_counts().index[0])


def make_candidates(f: pd.DataFrame) -> pd.DataFrame:
    """f: feature table (one snap or several; uses column 'snap'). Returns long candidate table with row index 'rid'."""
    f = f.reset_index(drop=True)
    hm = pd.concat([hist_mode(s).rename("hmode").to_frame().assign(snap=s) for s in f["snap"].unique()]).reset_index()
    hmv = f[["entity_id", "snap"]].merge(hm, on=["entity_id", "snap"], how="left")["hmode"].values
    cols = {
        "l1": f["l1_cand"].values, "l2": f["l2_cand"].values, "mall": f["mall_cand"].values, "peer": f["peer_cand"].values,
        "ps": f["ps_cand"].values, "psl1": f["ps_l1"].values, "hmode": hmv,
        "closed": np.array(["closed"] * len(f), dtype=object),
        "reg": np.where(f["shown"] != f["reg"], f["reg"], None),
    }
    srcs = list(SOURCES)
    if "l1t_cand" in f.columns:  # oracle experiment only (oracle_l1truth.py): last season's TRUE hours
        srcs.append("l1t")
        cols["l1t"] = f["l1t_cand"].values
    parts = []
    for s in srcs:
        v = pd.Series(cols[s], dtype=object)
        ok = v.notna() & (v.astype(str) != "None") & (v.astype(str) != "nan")
        parts.append(pd.DataFrame({"rid": np.where(ok)[0], "cand": v[ok].values, "src": s}))
    c = pd.concat(parts, ignore_index=True)
    c["shown"] = f["shown"].values[c["rid"]]
    c = c[c["cand"] != c["shown"]]
    # one row per (rid, cand) with source flags
    flags = pd.crosstab([c["rid"], c["cand"]], c["src"]).reindex(columns=srcs, fill_value=0).clip(upper=1)
    flags.columns = [f"from_{s}" for s in srcs]
    flags = flags.reset_index()
    flags["n_sources"] = flags[[f"from_{s}" for s in srcs if s not in ("closed", "l1t")]].sum(axis=1)
    st = np.array([hstat(x) for x in flags["cand"].tolist()], dtype=float)
    flags["cand_closed"], flags["cand_open"], flags["cand_close"], flags["cand_min"], flags["cand_niv"] = st.T
    reg_open = f["reg_open"].values[flags["rid"]]
    reg_close = f["reg_close"].values[flags["rid"]]
    reg_min = f["reg_min"].values[flags["rid"]]
    flags["cand_open_delta"] = flags["cand_open"] - reg_open
    flags["cand_close_delta"] = flags["cand_close"] - reg_close
    flags["cand_min_ratio"] = flags["cand_min"] / np.where(reg_min > 0, reg_min, np.nan)
    shown_closed = f["shown_closed"].values[flags["rid"]]
    flags["cand_state_flip"] = (flags["cand_closed"] != shown_closed).astype(float)
    return flags




def payoff_matrix(c: pd.DataFrame) -> np.ndarray:
    """(n, 5) payoffs of SUGGEST cand for each outcome class."""
    shown_state_same = 1 - c["cand_state_flip"].values
    P = np.tile(np.array([1.0, 0.6, 0.2, -1.0, 0.0]), (len(c), 1))
    P[:, 4] = np.where(shown_state_same == 1, -0.5, -1.0)
    return P
