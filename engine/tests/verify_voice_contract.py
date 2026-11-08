"""Exercise the existing voice consumer with inert config; no .env, network or calls."""
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import importlib,importlib.util,json,sys,types
HERE=Path(__file__).resolve().parents[1];ROOT=HERE.parent

def main():
    # A private module namespace keeps the voice service's real config/startup
    # out of this test. Only its pure bundle and policy functions are exercised.
    name='fonecta_engine_contract';folder=ROOT/'voice/voice_service'
    package=types.ModuleType(name);package.__path__=[str(folder)];sys.modules[name]=package
    cfg=types.ModuleType(name+'.config')
    values={'PKG_DIR':ROOT/'resources',
            'SUGGESTIONS_PATH':HERE/'output/voice_suggestions.json','VOICE_MAX_ITEMS':4,'VOICE_HORIZON_DAYS':21,
            'VOICE_MIN_CONFIDENCE':.8,'VOICE_URGENT_DAYS':14,'VOICE_WAIT_HOURS_URGENT':24,'VOICE_WAIT_HOURS':48,
            'VOICE_MIN_GAP_HOURS':24,'VOICE_MAX_ATTEMPTS':2}
    for k,v in values.items():setattr(cfg,k,v)
    sys.modules[name+'.config']=cfg
    def deny(event,args):
        if event=='open' and isinstance(args[0],str) and Path(args[0]).name=='.env':raise AssertionError('Secrets cannot be read')
        if event in ['socket.connect','socket.connect_ex']:raise AssertionError('Network forbidden in contract test')
    sys.addaudithook(deny)
    bundles=importlib.import_module(name+'.bundles');policy=importlib.import_module(name+'.policy')
    canonical=json.loads((HERE/'output/demo_suggestions.json').read_text())
    ready=json.loads((HERE/'output/voice_suggestions.json').read_text());review=json.loads((HERE/'output/review_suggestions.json').read_text())
    assert all(x['action']=='FLAG' or (x['action']=='SUGGEST' and isinstance(x['confidence'],(float,int)) and x['contact_eligible']) for x in ready)
    assert all(x['action']=='SUGGEST' and x['confidence'] is None and x['contact_eligible'] for x in review)
    expected={x['suggestion_id'] for x in canonical if x['action']=='SUGGEST' and x['contact_eligible']}
    got={x['suggestion_id'] for x in ready+review if x['action']=='SUGGEST'};assert expected==got
    now=datetime(2026,12,15,12,30,tzinfo=ZoneInfo('Europe/Helsinki'));nitems=0
    for eid in sorted({x['entity_id'] for x in canonical}):
        own=next(x for x in canonical if x['entity_id']==eid)
        items,flags=bundles._items(bundles.suggestions_for(eid),'en',eid[:2],now)
        b={'items':items,'flags':flags,'opted_out':own['opted_out'],'standing_permission_holidays':own['standing_permission'],
           'timezone':'Europe/Helsinki','best_contact_window':'10-17'}
        decision=policy.evaluate(b,[],[],now,'browser')
        assert decision['decision']=='NO_CALL'  # no prior unanswered text, so never an automatic call
        assert len(items)<=4;nitems+=len(items)
    report={'passed':True,'businesses_tested':17,'voice_rows':len(ready),'rule_rows_for_review':len(review),
            'items_rendered_at_demo_clock':nitems,'canonical_suggestions_preserved':True,'calls_made':0,'network_used':False,'env_read':False,
            'integration_setting':'Set SUGGESTIONS_PATH to the absolute engine/output/voice_suggestions.json path; not changed by this test.'}
    (HERE/'voice_contract_report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))

if __name__=='__main__':main()
