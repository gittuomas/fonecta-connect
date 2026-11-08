"""Selected-output integration and the existing owner decision flow, using only the local mock."""
import csv
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from step5_apply.api import Step5

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from predictions import SelectedPredictions


def week(start='09:00',end='17:00'):
    return {d:{'openIntervals':[{'start':start,'end':end}]} for d in
            ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']}


class OutputContract(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'demo_suggestions.json'
        self.targets={('TEST','2026-12-24'):{'day_key':'XMAS_EVE'}}
        self.entities={'TEST':{'meta':{'countryCode':'FI'},'hours':week()}}
        self.history={('TEST','2025-12-24'):[{'entered_at':'2025-12-01','holidayHours':{'openIntervals':[{'start':'09:00','end':'17:00'}]}}]}
        self.regular={'TEST':[{'effective_from':'before-2024','hours':week()}]}
        self.calendar=[{'country':'FI','day_key':'XMAS_EVE','date':'2025-12-24'}]

    def create(self, rows=None):
        records=[{'entity_id':eid,'date':day,'action':action,'proposed_hours':hours or None,
                  'day_key':'XMAS_EVE','shown_hours':'10:00-18:00','model_id':'test',
                  'contact_eligible':action=='SUGGEST','opted_out':False}
                 for eid,day,action,hours in (rows if rows is not None else [('TEST','2026-12-24','SUGGEST','09:00-17:00')])]
        self.path.write_text(json.dumps(records),encoding='utf-8')
        return SelectedPredictions(self.path,self.targets,self.entities,self.history,self.regular,self.calendar)

    def test_own_history_is_named_only_when_it_matches(self):
        p=self.create().predict('TEST',date(2026,12,24))
        self.assertEqual(p['last_hours'],'09:00-17:00')
        p=self.create([('TEST','2026-12-24','SUGGEST','closed')]).predict('TEST',date(2026,12,24))
        self.assertIsNone(p['last_hours']);self.assertEqual(p['basis'],'SELECTED_MODEL')

    def test_last_year_regular_means_last_year_schedule(self):
        self.history[('TEST','2025-12-24')][0]['holidayHours']={'isRegularHours':True}
        self.entities['TEST']['hours']=week('10:00','18:00')
        self.regular['TEST'].append({'effective_from':'2026-01-01','hours':week('10:00','18:00')})
        p=self.create([('TEST','2026-12-24','SUGGEST','10:00-18:00')]).predict('TEST',date(2026,12,24))
        self.assertIsNone(p['last_hours'])
        self.assertEqual(p['shown'],'10:00-18:00')

    def test_later_history_is_not_available(self):
        self.history[('TEST','2025-12-24')][0]['entered_at']='2026-11-08'
        self.assertIsNone(self.create().predict('TEST',date(2026,12,24))['last_hours'])

    def test_missing_duplicate_unknown_and_bad_hours_fail_closed(self):
        for rows in ([], [('TEST','2026-12-24','SUGGEST','bad')],
                     [('TEST','2026-12-24','SUGGEST','09:00-17:00')]*2,
                     [('TEST','2026-12-25','NO_ACTION','')],
                     [('TEST','2026-12-24','UNKNOWN','')],
                     [('TEST','2026-12-24','NO_ACTION','closed')]):
            with self.subTest(rows=rows),self.assertRaises(ValueError):self.create(rows)

    def test_overnight_split_and_flag_are_supported(self):
        for action,hours in [('SUGGEST','15:00-03:00'),('SUGGEST','11:00-14:00,17:00-22:00'),('FLAG','')]:
            p=self.create([('TEST','2026-12-24',action,hours)]).predict('TEST',date(2026,12,24))
            self.assertEqual((p['action'],p['hours']),(action,hours))

    def test_no_fallback_outside_export(self):
        with self.assertRaisesRegex(ValueError,'outside'):
            self.create().predict('TEST',date(2027,12,24))


class OwnerFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.s5=Step5.from_env(use_sandbox=False,runtime_dir=cls.tmp.name,today=date(2026,11,6))
        spec=importlib.util.spec_from_file_location('owner_test_app',HERE/'app.py')
        cls.app=importlib.util.module_from_spec(spec)
        with patch.object(Step5,'from_env',return_value=cls.s5),patch.dict(os.environ,{'CONNECT_LIVE_CHECKS':'', 'CONNECT_SUGGESTIONS_PATH':str(HERE.parent/'Solution/engine/output/demo_suggestions.json')}):
            spec.loader.exec_module(cls.app)
        cls.app.LEARN=cls.app.learning.Learner(str(Path(cls.tmp.name)/'learning.json'))
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),cls.app.App)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base='http://127.0.0.1:'+str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join()
        cls.s5.close();cls.tmp.cleanup()

    def setUp(self):
        self.s5.mock.reset()
        for name in ('SEEN','DECISIONS','DELIVERY','CHECK_SESSIONS','WA','WA_CHAT','WA_DONE','WA_AUTO','WA_MORE','REMIND','NOREPLY','EDIT_LEARNED','WELCOME_CHAT','READER'):
            getattr(self.app,name).clear()
        self.app.LEARN.reset()
        for fn in ('suggestion','bundles_for','opted_out','_value'):getattr(self.app,fn).cache_clear()
        ai=patch.object(self.app.hours_ai,'read_reply',return_value=None);ai.start();self.addCleanup(ai.stop)

    def request(self,path,form=None):
        data=urllib.parse.urlencode(form).encode() if form is not None else None
        with urllib.request.urlopen(self.base+path,data=data,timeout=10) as r:return r.read().decode('utf-8')

    def test_all_demo_decisions_match_export_without_baseline_calls(self):
        with patch.object(self.app.BL,'explain',side_effect=AssertionError('baseline must not decide')):
            for (eid,ds),row in self.app.PREDICTIONS.rows.items():
                if eid in self.app.DEMO:
                    p=self.app.suggestion(eid,date.fromisoformat(ds))
                    self.assertEqual((p['action'],p['hours']),(row['action'],row['hours']))

    def test_new_customer_gets_selected_hours_and_existing_message(self):
        p=self.app.suggestion('FI-D0017',date(2026,12,24))
        self.assertEqual((p['action'],p['hours'],p['last_hours']),('SUGGEST','closed',None))
        html=self.request('/w/FI-D0017/christmas')
        for text in ('We suggest:', 'No, keep as it is', 'Other hours', 'We never ask for logins or passwords.'):
            self.assertIn(text,html)
        self.assertNotIn('Last year you were open:',html)

    def test_existing_customer_keeps_own_history_message(self):
        # Test the C13 history wording independently of the separate Instagram demo,
        # whose owner-announced dates now deliberately supersede this holiday bundle.
        with patch.object(self.app, 'instagram_days', return_value=set()):
            html=self.request('/w/FI-D0001/christmas')
        self.assertIn('Last year you were open:',html)
        self.assertIn('7:30–13',html)

    def test_instagram_announced_days_do_not_get_duplicate_holiday_message(self):
        self.assertNotIn('christmas', [b[0] for b in self.app.bundles_for('FI-D0001')])
        self.assertIn('Instagram', self.request('/ig/FI-D0001'))

    def test_existing_sms_email_and_push_receive_engine_hours(self):
        for channel in ('s','e','p'):
            with self.subTest(channel=channel):
                html=self.request('/'+channel+'/FI-D0017/christmas')
                self.assertIn('closed',html.lower())
                self.assertNotIn('Last year you were open:',html)

    def test_approve_repeat_and_undo_through_existing_chat(self):
        path='/w/FI-D0017/christmas';self.request(path)
        self.request(path,{'a':'yes'})
        self.assertEqual(self.s5.snapshot('FI-D0017',['2026-12-24'])['2026-12-24'],'closed')
        puts=len(self.s5.mock.module.STORE.update_log)
        self.request(path,{'a':'yes'})
        self.assertEqual(len(self.s5.mock.module.STORE.update_log),puts)
        self.request(path+'/reply',{'msg':'UNDO'})
        self.assertIsNone(self.s5.snapshot('FI-D0017',['2026-12-24'])['2026-12-24'])

    def test_decline_never_writes(self):
        path='/w/FI-D0017/christmas';self.request(path)
        count=len(self.s5.mock.module.STORE.update_log)
        self.request(path,{'a':'no'})
        self.assertEqual(len(self.s5.mock.module.STORE.update_log),count)

    def test_other_hours_only_changes_the_requested_date(self):
        path='/w/FI-D0017/christmas';self.request(path)
        self.request(path,{'a':'other'})
        self.request(path+'/reply',{'msg':'24 Dec 10-14'})
        self.assertEqual(self.s5.snapshot('FI-D0017',['2026-12-24','2026-12-25']),
                         {'2026-12-24':'10:00-14:00','2026-12-25':None})

    def test_silence_flags_and_unsupported_dates(self):
        self.assertIn('No message',self.request('/m/FI-D0013/2026-12-24'))
        eid,ds=next(k for k,v in self.app.PREDICTIONS.rows.items() if v['action']=='FLAG')
        self.assertIn('Fonecta person',self.request('/m/'+eid+'/'+ds))
        with self.assertRaises(urllib.error.HTTPError) as err:self.request('/m/FI-D0017/2027-12-24')
        self.assertEqual(err.exception.code,404)

    def test_owner_preferences_survive_engine_integration(self):
        path='/w/FI-D0017/christmas';self.request(path)
        self.request(path,{'a':'no'})
        self.assertIn('Would you like suggestions',self.request(path))
        self.assertIn('holiday',self.app.LEARN._load()['FI-D0017']['scores'])
        self.request(path+'/reply',{'msg':'STOP'})
        self.assertTrue(self.app.LEARN._load()['FI-D0017']['stopped'])
        self.request(path+'/reply',{'msg':'START'})
        self.assertFalse(self.app.LEARN._load()['FI-D0017']['stopped'])

    def test_ineligible_suggestions_cannot_be_opened_or_bundled(self):
        key=('FI-D0017','2026-12-24');row=self.app.PREDICTIONS.rows[key]
        with patch.dict(row,{'contact_eligible':False}):
            with self.assertRaises(urllib.error.HTTPError) as err:self.request('/m/FI-D0017/2026-12-24')
            self.assertEqual(err.exception.code,404)
            self.assertFalse(any(s['day'].isoformat()==key[1] for _,_,items in self.app.bundles_for(key[0]) for s in items))


if __name__=='__main__':unittest.main()
