"""Frozen C13 inference from public rows and exported learned artifacts."""
import json
import numpy as np
import pandas as pd
import lightgbm as lgb
import engine_context as H
import features as F
import cands as C
import frozen_cp
import crowd
from t1 import adapter
from overlay import structural_overlay,overlay_values
from gated import abstain_overlay
from regspec import spec_fill

def rows_stack(ctx,frame,folder):
    f=adapter.extra_rows(ctx,frozen_cp.extra_rows(ctx,frame))
    tab=pd.read_parquet(folder/'past_day_state.parquet').rename(columns={'tseason':'season'})
    f=f.merge(tab,on=['entity_id','day_key','season','snap'],how='left',validate='many_to_one',sort=False)
    assert len(f)==len(frame)
    return crowd._attach(ctx,f,False)

def candidates(frame):
    fc=[x for x in F.feature_columns(frame) if x not in ['src','is_ss']]
    c=crowd.extra_cands_crowd(adapter.extra_cands(C.make_candidates(frame),frame),frame)
    x=frame.iloc[c.rid.to_numpy()][fc+['entity_id','date']].reset_index(drop=True)
    out=pd.concat([c.reset_index(drop=True),x],axis=1)
    for col in F.CAT_COLS:out[col]=pd.Categorical(out[col].astype(str))
    return out

def decide(te,ep,ctx,threshold):
    best=te.assign(ep=ep).sort_values('ep',ascending=False).groupby('rid',as_index=False).head(1)
    sug=best[best.ep>threshold][['entity_id','date','cand','ep']].rename(columns={'cand':'hours','ep':'confidence'})
    sug['action']='SUGGEST';flags=ctx.flags();keys=set(zip(flags.entity_id,flags.date))
    sug=sug[[k not in keys for k in zip(sug.entity_id,sug.date)]]
    return pd.concat([flags.assign(confidence=np.nan),sug],ignore_index=True)

def predict_partition(ctx,folder,config):
    meta=json.loads((folder/'metadata.json').read_text())
    f=rows_stack(ctx,ctx.predict_rows(),folder);te=candidates(f);cols=meta['feature_columns']
    cls=lgb.Booster(model_str=(folder/'classifier.txt').read_text(encoding='utf-8'));reg=lgb.Booster(model_str=(folder/'payoff.txt').read_text(encoding='utf-8'))
    raw=cls.predict(te[cols],num_threads=config['threads']);prob=np.zeros((len(te),5));prob[:,meta['classes']]=raw
    cp=(prob*C.payoff_matrix(te)).sum(axis=1);rp=reg.predict(te[cols],num_threads=config['threads'])
    ep=config['classifier_weight']*cp+config['payoff_weight']*rp
    base=decide(te,ep,ctx,config['suggest_threshold'])
    if config['centre_mode']=='override':structural=structural_overlay(ctx,base,arms=tuple(config['structural_arms']))
    elif config['centre_mode']=='abstain':
        structural=structural_overlay(ctx,base,arms=tuple(a for a in config['structural_arms'] if a!='centre'))
        structural=abstain_overlay(ctx,structural,arms=('centre',))
    else:raise ValueError('Unknown centre mode')
    result=H.finalize(spec_fill(ctx,structural,gate=config['weekly_spec_gate']),ctx)
    te['class_ep']=cp;te['reg_ep']=rp;te['expected_points']=ep;te['p_exact']=prob[:,0];te['p_change']=1-prob[:,4]
    return result,{'candidates':te,'features':f,'base':base,'structural':structural,'overlay':overlay_values(ctx,tuple(config['structural_arms']))}
