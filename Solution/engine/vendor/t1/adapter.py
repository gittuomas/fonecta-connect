"""T1 any-lag features at each query snapshot; no shared or global cache.

Local frozen original matching/candidate logic. Source entries and their REGULAR
resolution come from a copied sanctioned context at each row's own snapshot.
The explicit identity is (entity_id,date,snap), including merge validation.
"""
from copy import copy
import pandas as pd
from . import frozen_legacy as original

IDENTITY = ['entity_id', 'date', 'snap']
KEY = original.KEY + ['snap']


class _RowsAtSnapshot:
    def __init__(self, ctx, snap, rows):
        self._ctx = copy(ctx)
        self._ctx.snap = str(snap)
        self._rows = rows[KEY].copy()

    def entries(self):
        return self._ctx.entries()

    def predict_rows(self):
        return self._rows.copy()

    def train_rows(self):
        return self._rows.iloc[:0].copy()


def build_for_rows(ctx, frame):
    rows = frame[KEY].copy()
    if rows[IDENTITY].isna().any().any():
        raise ValueError('Missing query identity or snapshot')
    if (rows['snap'].astype(str) > str(ctx.snap)).any():
        raise ValueError('Query snapshot is later than the supplied context')
    # One source resolution per query snapshot, before matching or pooling.
    # No harness.shared: its key omits snapshot and imported helper hashes.
    pieces = []
    for snap, group in rows.groupby('snap', sort=True):
        distinct = group.drop_duplicates(IDENTITY)
        if len(distinct) != len(group.drop_duplicates(KEY)):
            raise ValueError('Conflicting query fields for the same identity')
        table = original.build_anylag(_RowsAtSnapshot(ctx, snap, distinct))
        table['snap'] = snap
        pieces.append(table)
    if not pieces:
        # The original builder has no empty-row contract. Preserve its schema.
        cols = ['entity_id','date','al_mrl','al_has','al_raw','al_cand','al_eq_shown',
                'al_ndistinct','al_ncand','al_eq12','al_eq13','al_eq23']
        cols += [f'al_l{lag}_{w}' for lag in range(1, original.MAXLAG+1) for w in ('has','closed','re')]
        return pd.DataFrame(columns=cols+['snap'])
    # Reconstruct from records, as the original builder does for the whole
    # table. concat alone makes mixed all-null/string partitions object dtype.
    out = pd.DataFrame.from_records([r for piece in pieces for r in piece.to_dict('records')])
    if out.duplicated(IDENTITY).any():
        raise ValueError('Duplicate snapshot-aware feature identity')
    return out


def build_anylag(ctx):
    rows = pd.concat([ctx.predict_rows()[KEY], ctx.train_rows()[KEY]], ignore_index=True)
    return build_for_rows(ctx, rows)


def extra_rows(ctx, frame):
    table = build_for_rows(ctx, frame)
    cols = IDENTITY + original.FEATCOLS + original.HIDDEN
    result = frame.merge(table[cols], on=IDENTITY, how='left', validate='many_to_one', sort=False)
    return result.rename(columns={c: f'lab_{c}' for c in original.HIDDEN})


def extra_cands(candidates, frame):
    if frame.empty:
        # The legacy _pairs helper constructs a float mask for zero rows.
        # Preserve the expected schema without entering that undefined path.
        out = candidates.copy()
        out['from_alag'] = 0.0
        return out
    return original.extra_cands_habit(candidates, frame)
