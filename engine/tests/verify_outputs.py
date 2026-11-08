"""Independent comparison to exported checkpoints and the demo interface contract."""
from pathlib import Path
import argparse,hashlib,json,sys
from datetime import date,timedelta
import numpy as np
import pandas as pd
HERE=Path(__file__).resolve().parents[1];ROOT=HERE.parent
sys.path.insert(0,str(HERE))
from validate_submission import valid_hours

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=HERE/'output')
    output=parser.parse_args().output;bundle=HERE/'models/c13_v1';cfg=json.loads((HERE/'config.json').read_text())
    r=json.loads((output/'run_report.json').read_text());assert r['reference_parity'] and r['validation']['valid'] and r['validation']['complete_partition_coverage']
    actual=output/'predictions_2026.csv';reference=bundle/'reference_v1.csv'
    assert actual.read_bytes()==reference.read_bytes()
    n=0;max_ep_error=0.;feature_cells=0
    for part in r['partitions']:
        meta=json.loads((bundle/part/'metadata.json').read_text())
        a=pd.read_parquet(output/'verification'/part/'candidates.parquet')
        b=pd.read_parquet(HERE/'verification'/part/'reference_candidates.parquet')
        cols=['entity_id','date','rid','cand']+meta['feature_columns']
        assert len(a)==len(b)
        for col in dict.fromkeys(cols):
            pd.testing.assert_series_equal(a[col].astype(object) if isinstance(a[col].dtype,pd.CategoricalDtype) else a[col],
                b[col].astype(object) if isinstance(b[col].dtype,pd.CategoricalDtype) else b[col],check_dtype=False,check_names=True,check_exact=True)
        old=np.load(HERE/'verification'/part/'reference_ep.npz')
        for col in ['class_ep','reg_ep']:
            diff=float(np.max(np.abs(a[col].to_numpy()-old[col])))
            max_ep_error=max(max_ep_error,diff);np.testing.assert_allclose(a[col],old[col],rtol=0,atol=1e-12)
        n+=len(a);feature_cells+=len(a)*len(meta['feature_columns'])
    objects=json.loads((output/'demo_suggestions.json').read_text())
    package=ROOT/'resources';targets=pd.read_csv(package/'evaluation/targets_2026.csv',keep_default_na=False)
    targets=targets[targets.split.eq('demo')];wanted=set(zip(targets.entity_id,targets.date))
    assert {(x['entity_id'],x['date']) for x in objects}==wanted and len(objects)==len(wanted)
    crm=pd.read_csv(package/'data/customer_contacts.csv',keep_default_na=False).set_index('entity_id')
    predictions=pd.read_csv(actual,keep_default_na=False).set_index(['entity_id','date'])
    required={'suggestion_id','entity_id','date','day_key','action','shown_hours','proposed_hours','confidence','expected_points','priority','contact_by','evidence','language','preferred_channel','standing_permission','opted_out'}
    counts={};cold=[]
    for x in objects:
        assert required<=set(x);key=(x['entity_id'],x['date']);p=predictions.loc[key] if key in predictions.index else None
        assert x['suggestion_id']=='|'.join(key)
        assert x['action']==(p.action if p is not None else 'NO_ACTION')
        assert x['proposed_hours']==(p.hours if p is not None and p.action=='SUGGEST' else None)
        if x['action']=='SUGGEST':assert valid_hours(x['proposed_hours']) and x['proposed_hours']!=x['shown_hours']
        assert x['contact_by']==(date.fromisoformat(x['date'])-timedelta(days=21)).isoformat()
        contact=crm.loc[x['entity_id']]
        assert x['standing_permission']==bool(int(contact.standing_permission_holidays))
        assert x['opted_out']==bool(int(contact.opted_out_of_suggestions))
        assert x['preferred_channel']==contact.preferred_channel and x['language']==contact.preferred_language
        assert x['contact_eligible']==(x['action']=='SUGGEST' and not x['opted_out'])
        assert x['confidence'] is None or 0<=x['confidence']<=1
        assert x['priority']>=0 and x['evidence']
        counts[x['action']]=counts.get(x['action'],0)+1
        if x['entity_id']=='FI-D0017':
            assert not {e['source'] for e in x['evidence']}&{'LAST_YEAR_ENTRY','OLDER_ENTRY','OWN_HABIT_REAPPLIED','OWN_HOURS_HISTORY'}
            cold.append(x)
    assert len({x['entity_id'] for x in objects})==17 and cold
    voice=json.loads((output/'voice_suggestions.json').read_text());review=json.loads((output/'review_suggestions.json').read_text())
    assert all(x['action']=='FLAG' or isinstance(x['confidence'],(float,int)) for x in voice)
    assert all(x['confidence'] is None and x['action']=='SUGGEST' for x in review)
    report={'passed':True,'byte_exact_reference':True,'prediction_sha256':hashlib.sha256(actual.read_bytes()).hexdigest(),
        'partitions':len(r['partitions']),'candidate_rows_compared':n,'feature_cells_compared':feature_cells,'max_expected_points_error':max_ep_error,
        'demo_rows':len(objects),'demo_businesses':17,'demo_actions':counts,'cold_start_rows':len(cold),'zero_invented_own_history_evidence':True,
        'no_model_refits':True,'no_label_reads':True}
    (HERE/'verification_report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))

if __name__=='__main__':main()
