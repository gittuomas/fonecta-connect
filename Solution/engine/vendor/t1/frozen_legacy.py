import numpy as np
import pandas as pd
from .habit_pure import classify, encode, apply as happly

MAXLAG = 4

SKIP_KEYS = ('EVENT', 'SCHOOL_BREAK')

KEY = ['entity_id', 'date', 'day_key', 'season', 'rank', 'reg', 'shown']

def _nn(v):
    return not (v is None or v is pd.NaT or (isinstance(v, float) and np.isnan(v)))

def _reapply(src_res, src_reg, tgt_reg):
    """Encode the source day's outcome against its own weekday regular hours, render on the target weekday's."""
    if not (_nn(src_res) and _nn(src_reg) and _nn(tgt_reg)):
        return None
    try:
        cls, _, _ = classify(src_res, src_reg if _nn(src_reg) else 'closed')
        return happly(encode(cls, src_res, src_reg), tgt_reg)
    except Exception:
        return None

def build_anylag(ctx):
    """Per (entity_id, date): the most recent same-day_key entry at ANY lag, matched on history-file dates.

    Columns: al_mrl (0 = none), al_has, al_cand (habit re-apply of the most recent entry on the target weekday),
    al_raw (its resolved string), al_l{1..4}_has / _closed / _re, pairwise agreement, al_ndistinct, al_eq_shown.
    No truth is used, so the table is safe to share across folds.
    """
    from features import wseason
    ea = ctx.entries()
    ea = ea[~ea['day_key'].isin(SKIP_KEYS)].copy()
    ea['eseason'] = wseason(ea['date'])
    ea = ea.sort_values('date')
    ea['rank_h'] = ea.groupby(['entity_id', 'day_key', 'eseason']).cumcount()
    idx: dict = {}
    for e, k, s, r, d, res, rt in zip(ea['entity_id'], ea['day_key'], ea['eseason'], ea['rank_h'], ea['date'], ea['res'], ea['reg_then']):
        idx.setdefault((e, k, int(s)), []).append((int(r), d, res, rt))
    rows = pd.concat([ctx.predict_rows()[KEY], ctx.train_rows()[KEY]], ignore_index=True)
    rows = rows.drop_duplicates(['entity_id', 'date']).reset_index(drop=True)

    def pick(e, k, s, r):
        opts = idx.get((e, k, s))
        if not opts:
            return None
        for cand in opts:
            if cand[0] == r:
                return cand
        return min(opts, key=lambda x: abs(x[0] - r))
    out = []
    for e, d, k, s, rk, reg, shown in zip(rows['entity_id'], rows['date'], rows['day_key'], rows['season'], rows['rank'], rows['reg'], rows['shown']):
        rec = {'entity_id': e, 'date': d}
        s = int(s)
        rk = int(rk) if _nn(rk) else 0
        best = None
        if k in SKIP_KEYS:
            for lag in range(1, MAXLAG + 1):
                rec[f'al_l{lag}_has'] = 0.0
                rec[f'al_l{lag}_closed'] = np.nan
                rec[f'al_l{lag}_re'] = None
        else:
            for lag in range(1, MAXLAG + 1):
                p = pick(e, k, s - lag, rk)
                rec[f'al_l{lag}_has'] = float(p is not None)
                rec[f'al_l{lag}_closed'] = (1.0 if p[2] == 'closed' else 0.0) if p else np.nan
                rec[f'al_l{lag}_re'] = _reapply(p[2], p[3], reg) if p else None
                if p is not None and best is None:
                    best = (lag, p)
        rec['al_mrl'] = float(best[0]) if best else 0.0
        rec['al_has'] = float(best is not None)
        rec['al_raw'] = best[1][2] if best else None
        rec['al_cand'] = rec[f'al_l{best[0]}_re'] if best else None
        rec['al_eq_shown'] = float(rec['al_cand'] == shown) if _nn(rec['al_cand']) and _nn(shown) else np.nan
        vals = [rec[f'al_l{lag}_re'] for lag in range(1, MAXLAG + 1)]
        for a, b in ((1, 2), (1, 3), (2, 3)):
            va, vb = (vals[a - 1], vals[b - 1])
            rec[f'al_eq{a}{b}'] = float(va == vb) if _nn(va) and _nn(vb) else np.nan
        present = [v for v in vals if _nn(v)]
        rec['al_ndistinct'] = float(len(set(present))) if present else np.nan
        rec['al_ncand'] = float(len(present))
        out.append(rec)
    cols = ['entity_id', 'date', 'al_mrl', 'al_has', 'al_raw', 'al_cand', 'al_eq_shown', 'al_ndistinct', 'al_ncand', 'al_eq12', 'al_eq13', 'al_eq23'] + [f'al_l{lag}_{w}' for lag in range(1, MAXLAG + 1) for w in ('has', 'closed', 're')]
    return pd.DataFrame(out)[cols]

FEATCOLS = ['al_mrl', 'al_has', 'al_eq_shown', 'al_ndistinct', 'al_ncand', 'al_eq12', 'al_eq13', 'al_eq23'] + [f'al_l{lag}_{w}' for lag in range(1, MAXLAG) for w in ('has', 'closed')]

HIDDEN = ['al_cand', 'al_raw'] + [f'al_l{lag}_re' for lag in range(1, MAXLAG + 1)]

def _new_cand_frame(c, f, pairs, flag):
    """Build candidate rows for (rid, cand) pairs absent from c, with make_candidates' own feature tail."""
    from features import hstat
    if not len(pairs):
        return c
    have = set(zip(c['rid'].tolist(), c['cand'].tolist()))
    pairs = pairs.drop_duplicates(['rid', 'cand']).reset_index(drop=True)
    seen = np.array([(r, v) in have for r, v in zip(pairs['rid'], pairs['cand'])])
    hit = {(r, v) for r, v in zip(pairs.loc[seen, 'rid'], pairs.loc[seen, 'cand'])}
    m = np.array([(r, v) in hit for r, v in zip(c['rid'], c['cand'])])
    if m.any():
        c.loc[m, flag] = 1.0
        c.loc[m, 'n_sources'] = c.loc[m, 'n_sources'] + 1
    fresh = pairs[~seen]
    if not len(fresh):
        return c
    n = pd.DataFrame({'rid': fresh['rid'].values, 'cand': fresh['cand'].values})
    for col in c.columns:
        if col in ('rid', 'cand'):
            continue
        n[col] = 0.0
    n[flag] = 1.0
    n['n_sources'] = 1.0
    st = np.array([hstat(x) for x in n['cand'].tolist()], dtype=float)
    n['cand_closed'], n['cand_open'], n['cand_close'], n['cand_min'], n['cand_niv'] = st.T
    ro = f['reg_open'].values[n['rid'].values]
    rc = f['reg_close'].values[n['rid'].values]
    rm = f['reg_min'].values[n['rid'].values]
    n['cand_open_delta'] = n['cand_open'] - ro
    n['cand_close_delta'] = n['cand_close'] - rc
    n['cand_min_ratio'] = n['cand_min'] / np.where(rm > 0, rm, np.nan)
    sc = f['shown_closed'].values[n['rid'].values]
    n['cand_state_flip'] = (n['cand_closed'] != sc).astype(float)
    return pd.concat([c, n[c.columns]], ignore_index=True)

def _pairs(f, col):
    v = pd.Series(f[col].values, dtype=object)
    shown = pd.Series(f['shown'].values, dtype=object)
    ok = np.array([_nn(x) and str(x) not in ('None', 'nan') and (x != s) for x, s in zip(v, shown)])
    return pd.DataFrame({'rid': np.arange(len(f))[ok], 'cand': v[ok].values})

def extra_cands_habit(c, f):
    c = c.copy()
    c['from_alag'] = 0.0
    return _new_cand_frame(c, f, _pairs(f, 'lab_al_cand'), 'from_alag')

def extra_cands_both(c, f):
    c = c.copy()
    c['from_alag'] = 0.0
    c['from_alraw'] = 0.0
    c = _new_cand_frame(c, f, _pairs(f, 'lab_al_cand'), 'from_alag')
    return _new_cand_frame(c, f, _pairs(f, 'lab_al_raw'), 'from_alraw')
