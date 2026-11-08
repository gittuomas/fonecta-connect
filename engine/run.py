"""One-command frozen-snapshot inference. Reads public data and learned artifacts only."""
from pathlib import Path
import argparse,hashlib,json,os,runpy,sys,time
HERE=Path(__file__).resolve().parent;ROOT=HERE.parent
sys.dont_write_bytecode=True

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(4194304),b''):h.update(block)
    return h.hexdigest()

def canonical_csv(frame):
    import pandas as pd
    p=frame.reindex(columns=['entity_id','date','action','hours','confidence']).copy()
    p['hours']=p.hours.astype(object).where(p.hours.notna(),'');p.loc[p.action.eq('FLAG'),'hours']=''
    p['confidence']=pd.to_numeric(p.confidence,errors='coerce').round(4)
    return p.sort_values(['entity_id','date']).to_csv(index=False, lineterminator='\n')

def main():
    start=time.monotonic();ap=argparse.ArgumentParser(description=__doc__)
    default=ROOT/'resources'
    ap.add_argument('--data',type=Path,default=default/'data')
    ap.add_argument('--targets',type=Path,default=default/'evaluation/targets_2026.csv')
    ap.add_argument('--config',type=Path,default=HERE/'config.json')
    ap.add_argument('--output',type=Path,default=HERE/'output')
    ap.add_argument('--partitions',nargs='+',default=['cv0','cv1','cv2','cv3','cv4','sealed','final'])
    ap.add_argument('--save-verification',action='store_true')
    args=ap.parse_args();cfg=json.loads(args.config.read_text());bundle=HERE/cfg['bundle'];out=args.output.resolve()
    if out==ROOT or out.is_relative_to(default) or out.is_relative_to(args.data.resolve()):
        ap.error('Output must be separate from the repository root and read-only input package')
    assert cfg['snapshot']=='2026-11-06' and cfg['season']==2026,'This bundle is frozen to November2026'
    if cfg['classifier_weight']+cfg['payoff_weight']!=1:raise ValueError('Blend weights must sum to one')
    if len(set(args.partitions))!=len(args.partitions):raise ValueError('Repeated partition')
    for name,digest in json.loads((bundle/'public_inputs.json').read_text()).items():
        if sha(args.data/name)!=digest:raise ValueError(f'Public data changed: {name}; this frozen bundle needs a new authorized export')
    metadata={}
    for part in args.partitions:
        folder=bundle/part;manifest=folder/'manifest.json'
        if not manifest.exists():raise ValueError(f'Model partition {part} has not been exported yet')
        for name,digest in json.loads(manifest.read_text())['files'].items():
            if sha(folder/name)!=digest:raise ValueError(f'Corrupt model artifact: {part}/{name}')
        metadata[part]=json.loads((folder/'metadata.json').read_text())
        if metadata[part]['seed']!=cfg['seed'] or metadata[part]['snapshot']!=cfg['snapshot']:
            raise ValueError(f'Configuration does not match exported model partition {part}')
    for key in ['OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS']:os.environ.setdefault(key,str(cfg['threads']))
    out.mkdir(parents=True,exist_ok=True);cache=out/'cache';cache.mkdir(exist_ok=True)
    os.environ['FONECTA_ENGINE_DATA']=str(args.data.resolve());os.environ['FONECTA_ENGINE_CACHE']=str(cache)
    os.environ['FONECTA_ENGINE_MODEL']=str(bundle.resolve())
    sys.path.insert(0,str(HERE/'vendor'))
    runpy.run_path(str(HERE/'vendor/build_cache.py'),run_name='engine_cache_build')
    import numpy as np
    import pandas as pd
    import engine_context as H
    from inference import predict_partition
    from validate_submission import validate
    targets=pd.read_csv(args.targets,keep_default_na=False)
    if targets.duplicated(['entity_id','date']).any():raise ValueError('Duplicate target queries')
    selected_ids=set();pieces=[];demo_traces=[];allrows=H.public_rows(targets,cfg)
    for part in args.partitions:
        ids=set(metadata[part]['predict_ids'])
        if selected_ids&ids:raise ValueError('Overlapping model partitions')
        selected_ids|=ids
        ctx=H.Context(allrows,ids,cfg,part);pred,trace=predict_partition(ctx,bundle/part,cfg)
        pieces.append(pred)
        if part=='final':demo_traces.append((ctx,pred,trace))
        if args.save_verification:
            folder=out/'verification'/part;folder.mkdir(parents=True,exist_ok=True)
            trace['candidates'].to_parquet(folder/'candidates.parquet',index=False)
            trace['features'].to_parquet(folder/'features.parquet',index=False)
        print(f'inferred {part}: {len(pred)} actions',flush=True)
    pred=pd.concat(pieces,ignore_index=True)
    if pred.duplicated(['entity_id','date']).any():raise ValueError('Duplicate prediction identity')
    complete=set(args.partitions)=={'cv0','cv1','cv2','cv3','cv4','sealed','final'}
    if complete and not set(targets.entity_id)<=selected_ids:raise ValueError('Bundle does not cover all target businesses')
    path=out/('predictions_2026.csv' if complete else 'predictions_partial.csv');path.write_text(canonical_csv(pred), encoding='utf-8', newline='\n')
    guards={tuple(r[:2]):{'shown':r[2],'has_entry_now':r[3]} for r in allrows[['entity_id','date','shown','has_entry_now']].itertuples(index=False,name=None)}
    validation=validate(path,args.targets,guards,H.closed_entities());validation['complete_partition_coverage']=complete
    reference=pd.read_csv(bundle/'reference_v1.csv',keep_default_na=False)
    reference=reference[reference.entity_id.isin(selected_ids)]
    same=path.read_text()==canonical_csv(reference)
    if cfg['centre_mode']=='override' and cfg['model_id']=='cx_r02_c13_e01' and not same:
        raise AssertionError('Standalone output does not reproduce the frozen C13 v1 reference')
    if demo_traces:
        from suggestions import build_demo_suggestions,voice_handoff
        items=build_demo_suggestions(targets,demo_traces[0],cfg,args.data)
        (out/'demo_suggestions.json').write_text(json.dumps(items,ensure_ascii=False,indent=2,allow_nan=False)+'\n', encoding='utf-8', newline='\n')
        voice,review=voice_handoff(items)
        (out/'voice_suggestions.json').write_text(json.dumps(voice,ensure_ascii=False,indent=2,allow_nan=False)+'\n', encoding='utf-8', newline='\n')
        (out/'review_suggestions.json').write_text(json.dumps(review,ensure_ascii=False,indent=2,allow_nan=False)+'\n', encoding='utf-8', newline='\n')
    report={'model_id':cfg['model_id'],'snapshot':cfg['snapshot'],'runtime_seconds':time.monotonic()-start,
            'partitions':args.partitions,'validation':validation,'reference_parity':same,
            'prediction_sha256':sha(path),'labels_read':False,'lab_imports':False,
            'limitations':['Frozen November2026 roster and data fingerprint; refresh requires an authorized model export.','Confidence in the CSV is historical expected points, not a calibrated probability.']}
    (out/'run_report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2),flush=True)

if __name__=='__main__':main()
