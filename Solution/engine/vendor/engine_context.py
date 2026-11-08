"""Public-data inference context; intentionally has no labels, training or scoring API."""
from pathlib import Path
from functools import lru_cache
from types import SimpleNamespace
import os,json,re
import numpy as np
import pandas as pd
from common import DAYS,D,load_entities,load_reviews,load_publisher_suggestion_items,load_regular_history,csv
PKG=os.path.dirname(os.environ["FONECTA_ENGINE_DATA"])
SNAPS={2026:"2026-11-06"}
HOURS_RE=re.compile(r"^(closed|\d{2}:\d{2}-\d{2}:\d{2}(,\d{2}:\d{2}-\d{2}:\d{2})*)$")
def norm(s):
    s=(s or "").strip().lower().replace(" ","")
    return "closed" if s in ("","closed","kiinni","stängt","stengt") else s
EV=SimpleNamespace(norm=norm)
EDIT_COLUMNS=["id","publisherId","locationId","dateCreated","fieldName","item_type","item_date","sugg_hours","orig_hours","raw_suggested"]

_PUBLIC_MEMO={}
def shared(name,ctx,builder):
    if name!='t4_crowd':raise ValueError('Only the public crowd builder is permitted at inference')
    key=(name,ctx.snap)
    if key not in _PUBLIC_MEMO:_PUBLIC_MEMO[key]=builder(ctx)
    return _PUBLIC_MEMO[key].copy()

class Context:
    def __init__(self,rows,ids,config,partition):
        self.season=config['season'];self.snap=config['snapshot'];self.fold_key=partition
        self.predict_ids=frozenset(ids);self._allrows=rows;self.train_ids=frozenset()
    def predict_rows(self):
        return self._allrows[self._allrows.entity_id.isin(self.predict_ids)].reset_index(drop=True).copy()
    def train_rows(self):
        return self._allrows.iloc[:0].copy()
    def entries(self):
        from features import entries_at
        return entries_at(self.snap).copy()
    def reviews(self):
        r=load_reviews();return r[r.publisherDate<pd.Timestamp(self.snap)].copy()
    def edits(self):
        p=load_publisher_suggestion_items();return p.loc[p.dateCreated<self.snap,EDIT_COLUMNS].copy()
    def regular(self):
        r=load_regular_history();return r[(r.effective_from=='before-2024')|(r.effective_from<=self.snap)].copy()
    def statics(self):return load_entities().copy()
    def calendar(self):return csv('calendar_special_days.csv')
    def closed_flag(self):return closed_entities()
    def flags(self):
        if 'baseline' not in _PUBLIC_MEMO:
            from baseline_public import Baseline
            _PUBLIC_MEMO['baseline']=Baseline(self.season)
        baseline=_PUBLIC_MEMO['baseline']
        ids={e for e in baseline.closed_signal if not baseline.ents[e].get('closed')}
        out=self.predict_rows();out=out[out.entity_id.isin(ids)][['entity_id','date']]
        return out.assign(action='FLAG',hours='').reset_index(drop=True)

def public_rows(targets,config):
    from features import build_features,hstats_df
    f=build_features(targets,config['snapshot'])
    shown=exact_shown(f.entity_id.to_numpy(),f.date.to_numpy(),config['season'])
    f['shown']=shown
    st=hstats_df(pd.Series(shown,index=f.index,dtype=object),'shown')
    for c in st:f[c]=st[c].to_numpy()
    f['shown_eq_reg']=(f.shown==f.reg).astype(float)
    return f
@lru_cache(maxsize=None)
def _entities():
    from common import load_entities
    return load_entities()


@lru_cache(maxsize=None)
def closed_entities() -> frozenset:
    """Entities with closed: true in entities.json. 2026 state: never use for a past-season snapshot."""
    e = json.load(open(D("entities.json"), encoding="utf-8"))["response"]["entities"]
    return frozenset(x["meta"]["id"] for x in e if x.get("closed"))


@lru_cache(maxsize=None)
def _reg_version(snap: str) -> pd.DataFrame:
    from common import load_regular_history
    r = load_regular_history()
    r = r[(r["effective_from"] == "before-2024") | (r["effective_from"] <= snap)].copy()
    r["k"] = r["effective_from"].replace("before-2024", "0000-00-00")
    return r.sort_values("k").groupby("entity_id").tail(1).set_index("entity_id")[DAYS]


def exact_reg(entity_ids, dates, season: int) -> np.ndarray:
    """Published weekday hours as of the snapshot. 2026 uses entities.json, past seasons the regular-hours history."""
    src = _entities().set_index("entity_id")[DAYS] if season == 2026 else _reg_version(SNAPS[season])
    idx = src.index.get_indexer(entity_ids)
    wd = pd.to_datetime(pd.Series(dates)).dt.day_of_week.values
    vals = src.values
    return np.array([vals[i, w] if i >= 0 else None for i, w in zip(idx, wd)], dtype=object)


def exact_shown(entity_ids, dates, season: int) -> np.ndarray:
    from features import entries_at
    ea = entries_at(SNAPS[season])
    latest = dict(zip(zip(ea["entity_id"], ea["date"]), ea["hours"]))
    reg = exact_reg(entity_ids, dates, season)
    closed = closed_entities() if season == 2026 else frozenset()
    out = []
    for e, d, r in zip(entity_ids, dates, reg):
        if e in closed:
            out.append("closed")
            continue
        h = latest.get((e, d))
        out.append(h if (h is not None and h != "REGULAR") else r)
    return np.array(out, dtype=object)


def finalize(pred: pd.DataFrame, ctx: Context, guards=True) -> pd.DataFrame:
    """Validate and apply the standing guards from the handoff. Raises on anything that would break the real file."""
    need = {"entity_id", "date", "action"}
    if not need <= set(pred.columns):
        raise ValueError(f"fit_predict must return {need}, got {list(pred.columns)}")
    p = pred.copy()
    p["action"] = p["action"].astype(str).str.strip().str.upper()
    extra = set(p["entity_id"]) - ctx.predict_ids
    if extra:
        raise ValueError(f"predictions for {len(extra)} businesses outside ctx.predict_ids, e.g. {sorted(extra)[:3]}")
    if "hours" not in p:
        p["hours"] = ""
    p["hours"] = p["hours"].astype(object).where(p["hours"].notna(), "")
    p = p[p["action"].isin(["SUGGEST", "FLAG"])]
    if p.duplicated(["entity_id", "date"]).any():
        raise ValueError("duplicate (entity_id, date) in predictions")
    r = ctx.predict_rows()[["entity_id", "date", "shown", "has_entry_now"]]
    p = p.merge(r, on=["entity_id", "date"], how="inner")
    sug = p["action"] == "SUGGEST"
    p.loc[sug, "hours"] = p.loc[sug, "hours"].map(EV.norm)
    bad = p.loc[sug & ~p["hours"].map(lambda h: bool(HOURS_RE.match(h)) if isinstance(h, str) else False), "hours"]
    if len(bad):
        raise ValueError(f"invalid hours, e.g. {bad.iloc[0]!r}. One such row rejects the whole submission.")
    if guards:
        closed = ctx.closed_flag()
        drop = sug & (p["entity_id"].isin(closed) | (p["has_entry_now"] == 1) | (p["hours"] == p["shown"]))
        p = p[~drop]
    return p.drop(columns=["shown", "has_entry_now"]).reset_index(drop=True)

