"""Validate a candidate file without loading labels or computing a score."""
from pathlib import Path
import argparse,csv,json,math,re
from datetime import date

def valid_hours(value):
    if value=='closed':return True
    if not value:return False
    previous_end=-1
    for interval in value.split(','):
        if not re.fullmatch(r'\d{2}:\d{2}-\d{2}:\d{2}',interval):return False
        a,b=interval.split('-');ah,am=map(int,a.split(':'));bh,bm=map(int,b.split(':'))
        if not (0<=ah<24 and 0<=bh<24 and 0<=am<60 and 0<=bm<60):return False
        start=60*ah+am;end=60*bh+bm
        if end<=start:end+=1440
        if start<previous_end:return False
        previous_end=end
    return True

def validate(predictions,targets,guard_rows=None,closed_ids=()):
    with Path(targets).open(newline='',encoding='utf-8') as f:
        target_keys={(r['entity_id'],r['date']) for r in csv.DictReader(f)}
    errors=[];seen=set();counts={'SUGGEST':0,'FLAG':0,'NO_ACTION':0}
    with Path(predictions).open(newline='',encoding='utf-8') as f:
        reader=csv.DictReader(f);required={'entity_id','date','action','hours'}
        if not required.issubset(reader.fieldnames or []):raise ValueError('Missing required prediction columns')
        for n,r in enumerate(reader,2):
            key=(r['entity_id'],r['date']);action=r['action'];hours=r['hours']
            if key in seen:errors.append(f'line{n}: duplicate query')
            seen.add(key)
            if key not in target_keys:errors.append(f'line{n}: unknown target')
            try:date.fromisoformat(r['date'])
            except ValueError:errors.append(f'line{n}: invalid date')
            if action not in counts:errors.append(f'line{n}: invalid action');continue
            counts[action]+=1
            if action=='SUGGEST' and not valid_hours(hours):errors.append(f'line{n}: invalid suggested hours')
            if action!='SUGGEST' and hours:errors.append(f'line{n}: non-suggestion hours must be empty')
            if r.get('confidence'):
                try:
                    if not math.isfinite(float(r['confidence'])):raise ValueError()
                except ValueError:errors.append(f'line{n}: confidence must be finite or blank')
            if action=='SUGGEST' and guard_rows is not None:
                guard=guard_rows.get(key)
                if guard is None:errors.append(f'line{n}: missing guard context')
                elif key[0] in closed_ids or guard['has_entry_now']==1 or hours==guard['shown']:
                    errors.append(f'line{n}: standing guard violation')
    result={'valid':not errors,'prediction_rows':len(seen),'target_rows':len(target_keys),'implicit_no_action_rows':len(target_keys-seen),
            'actions':counts,'errors':errors,'label_access':False,'score_computed':False}
    if errors:raise ValueError(json.dumps(result,indent=2))
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--predictions',required=True);p.add_argument('--targets',required=True)
    a=p.parse_args();print(json.dumps(validate(a.predictions,a.targets),indent=2))
