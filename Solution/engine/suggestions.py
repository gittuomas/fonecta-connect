"""Demo delivery objects with source-grounded evidence and CRM preferences."""
import json,math
from datetime import date,timedelta
import numpy as np
import pandas as pd

def scalar(v):
    if v is None or pd.isna(v):return None
    return v.item() if isinstance(v,np.generic) else v

def build_demo_suggestions(targets,result,config,data):
    ctx,pred,trace=result
    demos=targets[targets.split.eq('demo')].copy()
    contacts=pd.read_csv(data/'customer_contacts.csv',keep_default_na=False).set_index('entity_id')
    entities=ctx.statics().set_index('entity_id')
    features=trace['features'].set_index(['entity_id','date'])
    actions=pred.set_index(['entity_id','date'])
    cand=trace['candidates'];cand=cand[cand.entity_id.isin(demos.entity_id)]
    candidates={(e,d,h):g.iloc[0] for (e,d,h),g in cand.groupby(['entity_id','date','cand'],observed=True)}
    overlay=trace['overlay'].set_index(['entity_id','date'])
    # Public observed demand only: 8-week mean for the target's weekday.
    a=pd.read_parquet(data/'analytics_daily.parquet',columns=['entity_id','date','GOOGLE_SEARCH_VIEWS'])
    a['date']=pd.to_datetime(a.date);snap=pd.Timestamp(config['snapshot'])
    a=a[(a.date<snap)&(a.date>=snap-pd.Timedelta(days=56))&a.entity_id.isin(demos.entity_id)]
    a['weekday']=a.date.dt.dayofweek
    demand=a.groupby(['entity_id','weekday']).GOOGLE_SEARCH_VIEWS.mean()
    output=[]
    source_names={'l1':'LAST_YEAR_ENTRY','l2':'OLDER_ENTRY','mall':'SHOPPING_CENTRE','peer':'PEER_HOURS',
                  'ps':'PUBLISHER_SUGGESTION','psl1':'PAST_PUBLISHER_SUGGESTION','hmode':'OWN_HOURS_HISTORY',
                  'alag':'OWN_HABIT_REAPPLIED','crowd':'PAST_DAY_PUBLIC_EVIDENCE','closed':'CLOSED_CANDIDATE','reg':'REGULAR_HOURS'}
    for row in demos.itertuples(index=False):
        key=(row.entity_id,row.date);f=features.loc[key]
        p=actions.loc[key] if key in actions.index else None
        action=p.action if p is not None else 'NO_ACTION';proposed=str(p.hours) if action=='SUGGEST' else None
        expected=None;confidence=None;evidence=[];basis='not_applicable'
        c=candidates.get((row.entity_id,row.date,proposed))
        if action=='SUGGEST':
            if c is not None and pd.notna(p.confidence):
                details={
                    'l1':f'Your entry for the matching holiday last season supports {proposed}.',
                    'l2':f'An older entry for this holiday supports {proposed}.',
                    'mall':f'Your shopping centre has published {proposed} for this date.',
                    'peer':f'Similar businesses\' published holiday hours support {proposed}.',
                    'ps':f'A publisher\'s proposed correction supports {proposed} for this date.',
                    'psl1':f'A publisher correction for the same holiday last season supports {proposed}.',
                    'hmode':f'You have used {proposed} most often for special opening hours.',
                    'alag':f'Your past holiday pattern, applied to this year\'s regular weekday hours, gives {proposed}.',
                    'crowd':f'A past holiday review or publisher correction, adjusted to your regular hours, suggests {proposed}.',
                    'reg':f'These match your published weekly hours for this weekday: {proposed}.'}
                for flag,label in source_names.items():
                    if flag in details and scalar(c.get('from_'+flag,0))==1:
                        evidence.append({'source':label,'detail':details[flag]})
                if not evidence:evidence.append({'source':'PUBLIC_HOLIDAY_PATTERNS','detail':f'Public holiday patterns suggest {proposed}; no owner-entered hours are recorded for this date yet.'})
            if p is not None and pd.notna(p.confidence) and c is not None:
                confidence=float(c.p_exact);expected=float(c.expected_points);basis='uncalibrated_classifier_exact_probability'
            else:
                basis='deterministic_rule_no_probability'
                if key in overlay.index and str(overlay.loc[key].ov_hours)==proposed:
                    origin=str(overlay.loc[key].ov_src)
                    reason={'terrace':'Your business is listed as a seasonal terrace, and no winter reopening is recorded.',
                            'school':f'Your latest published open school-break entry gives {proposed}.',
                            'centre':f'Your shopping centre\'s entry for this holiday last season suggests {proposed}, based on your past matching hours.'}[origin]
                    evidence.append({'source':'STRUCTURAL_'+origin.upper(),'detail':reason})
                else:evidence.append({'source':'REVIEW_WEEKLY_HOURS','detail':f'The latest review describing your weekly hours gives {proposed} for this weekday.'})
        elif action=='FLAG':
            evidence=[{'source':'CLOSURE_SIGNAL','detail':'Recent public reviews or a publisher edit indicate possible permanent closure; review is required.'}]
        else:
            if row.entity_id in ctx.closed_flag():reason='The business is already marked permanently closed.'
            elif f.has_entry_now==1:reason='Holiday hours were already entered before the snapshot.'
            else:reason='No suggestion survived the frozen decision rule and standing guards.'
            evidence=[{'source':'NO_ACTION','detail':reason}]
        crm=contacts.loc[row.entity_id] if row.entity_id in contacts.index else None
        opted=bool(crm is not None and str(crm.opted_out_of_suggestions)=='1')
        permission=bool(crm is not None and str(crm.standing_permission_holidays)=='1')
        language=str(crm.preferred_language) if crm is not None else str(entities.loc[row.entity_id].language)
        channel=str(crm.preferred_channel) if crm is not None else None
        views=float(demand.get((row.entity_id,pd.Timestamp(row.date).weekday()),0.))
        if not math.isfinite(views):views=0.
        contact=(date.fromisoformat(row.date)-timedelta(days=config['contact_lead_days'])).isoformat()
        output.append({'suggestion_id':f'{row.entity_id}|{row.date}','entity_id':row.entity_id,'date':row.date,'day_key':row.day_key,
            'action':action,'shown_hours':scalar(f.shown),'proposed_hours':proposed,'confidence':confidence,
            'confidence_basis':basis,'expected_points':expected,'priority':round(views,3) if action!='NO_ACTION' else 0.,
            'priority_basis':'public_same_weekday_search_views_8_week_mean','estimated_search_views':round(views,3),
            'contact_by':contact,'contact_overdue_at_snapshot':contact<config['snapshot'],'evidence':evidence,
            'language':language,'preferred_channel':channel,'standing_permission':permission,'opted_out':opted,
            'contact_eligible':action=='SUGGEST' and not opted,'review_required':action=='FLAG','model_id':config['model_id']})
    assert len(output)==len(demos) and len({x['suggestion_id'] for x in output})==len(output)
    assert len({x['entity_id'] for x in output})==17
    json.dumps(output,allow_nan=False)
    return output

def voice_handoff(objects):
    """Compatibility with the existing numeric-confidence voice consumer.

    Preserve all canonical objects separately. Rules without a probability go
    to review, never receive a fabricated confidence merely to trigger a call.
    """
    ready=[];review=[]
    for item in objects:
        if item['action']=='FLAG':ready.append(item)
        elif item['action']=='SUGGEST' and item['contact_eligible']:
            if item['confidence'] is None:review.append(item)
            else:ready.append(item)
    return ready,review
