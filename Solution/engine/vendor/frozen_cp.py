"""Torch-free reader with a unique module name, avoiding M0 features.py shadowing."""
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

import os
ROOT = Path(os.environ["FONECTA_ENGINE_MODEL"]) / "cp"
ANCHORS = ("XMAS_EVE", "XMAS_DAY", "BOXING_DAY", "NYE", "NEW_YEARS_DAY", "EPIPHANY", "DEC23", "INDEPENDENCE_DAY")
CACHE = {}


def snapshot(snap):
    if snap not in CACHE:
        p = ROOT / f"reconstruction_{snap}.npz"
        expected = json.loads((ROOT / "checkpoint_hashes.json").read_text())[p.name]
        assert hashlib.sha256(p.read_bytes()).hexdigest() == expected
        with np.load(p, allow_pickle=True) as saved:
            CACHE[snap] = {k: saved[k] for k in saved.files}
    return CACHE[snap]


def extra_rows(ctx, frame):
    result = frame.copy()
    columns = [f"x2_anchor_{k.lower()}" for k in ANCHORS] + ["x2_target_closed", "x2_target_regular",
        "x2_target_duration", "x2_target_observation", "x2_target_closed_l2", "x2_has_l1", "x2_has_l2", "x2_has_l3"]
    result[columns] = np.nan
    for snap, group in frame.groupby("snap", sort=True):
        assert str(snap) <= ctx.snap
        saved = snapshot(str(snap))
        recon, keys = saved["reconstruction"], saved["keys"].tolist()
        bi = pd.Index(saved["ids"]).get_indexer(group.entity_id)
        hi = pd.Index(keys).get_indexer(group.day_key)
        assert (bi >= 0).all()
        values = np.full((len(group), len(columns)), np.nan, dtype=np.float32)
        values[:, :8] = recon[bi[:, None], np.array([keys.index(k) for k in ANCHORS])[None, :], -1, 0]
        valid = hi >= 0
        values[valid, 8:12] = recon[bi[valid], hi[valid], -1, :]
        if recon.shape[2] > 1:
            values[valid, 12] = recon[bi[valid], hi[valid], -2, 0]
        for lag in (1, 2, 3):
            values[valid, 12+lag] = saved["observed"][bi[valid], hi[valid], -lag] if recon.shape[2] >= lag else 0
        result.loc[group.index, columns] = values
    return result
