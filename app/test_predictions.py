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
        with patch.object(Step5,'from_env',return_value=cls.s5),patch.dict(os.environ,{'CONNECT_LIVE_CHECKS':'', 'CONNECT_SUGGESTIONS_PATH':str(HERE.parent/'engine/output/demo_suggestions.json')}):
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
        for name in ('SEEN','PROPOSALS','PENDING_HOURS','CHAT_TIMES','DECISIONS','DELIVERY','CHECK_SESSIONS','WA','WA_CHAT','WA_DONE','WA_AUTO','WA_MORE','REMIND','NOREPLY','EDIT_LEARNED','WELCOME_CHAT','READER'):
            getattr(self.app,name).clear()
        self.app.LEARN.reset()
        for fn in ('suggestion','bundles_for','opted_out','_value'):getattr(self.app,fn).cache_clear()
        ai=patch.object(self.app.hours_ai,'configured',return_value=False);ai.start();self.addCleanup(ai.stop)

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

    def test_demo_entry_opens_local_new_customer(self):
        with urllib.request.urlopen(self.base + '/demo', timeout=10) as response:
            self.assertTrue(response.url.endswith('/w/FI-D0017/christmas'))
            self.assertIn('We suggest:', response.read().decode('utf-8'))

    def test_demo_entry_selects_only_a_mapped_sandbox_business(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        for mapping, expected in (({'FI-D0001': 'real-cafe'}, 'FI-D0001'),
                                  ({'FI-D0004': 'real-shop'}, 'FI-D0004')):
            with self.subTest(mapping=mapping), \
                    patch.object(self.s5.applier.primary, 'name', 'sandbox'), \
                    patch.object(self.s5.applier.primary, 'mapping', mapping, create=True):
                with self.assertRaises(urllib.error.HTTPError) as err:
                    opener.open(self.base + '/demo', timeout=10)
                self.assertEqual(err.exception.code, 303)
                self.assertEqual(err.exception.headers['Location'], f'/w/{expected}/christmas')

    def test_unmapped_business_offers_recovery_without_reading_or_writing(self):
        with patch.object(self.s5.applier.primary, 'name', 'sandbox'), \
                patch.object(self.s5.applier.primary, 'mapping', {'FI-D0001': 'real-cafe'}, create=True), \
                patch.object(self.s5, 'snapshot', side_effect=AssertionError('must not read unmapped entity')):
            with self.assertRaises(urllib.error.HTTPError) as err:
                self.request('/w/FI-D0017/christmas')
            self.assertEqual(err.exception.code, 404)
            self.assertIn("href='/demo'", err.exception.read().decode('utf-8'))
        self.assertEqual(self.s5.mock.module.STORE.update_log, [])

    def test_demo_entry_rejects_mapping_without_a_supplied_business(self):
        with patch.object(self.s5.applier.primary, 'name', 'sandbox'), \
                patch.object(self.s5.applier.primary, 'mapping', {'UNKNOWN': 'real-cafe'}, create=True):
            with self.assertRaises(urllib.error.HTTPError) as err:
                self.request('/demo')
            self.assertEqual(err.exception.code, 503)

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
        self.assertIsNone(self.s5.snapshot('FI-D0017',['2026-12-24'])['2026-12-24'])
        self.request(path+'/reply',{'msg':'CONFIRM'})
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



    def test_demo_success_and_undo_keep_product_copy(self):
        path = '/w/FI-D0017/christmas'
        self.request(path)
        html = self.request(path, {'a': 'yes'})
        self.assertIn('Done', html)
        self.assertIn('Publisher timings are estimates', html)
        self.assertNotIn('Local simulator only', html)
        self.assertNotIn('Public listings have not been', html)
        self.assertIn('Demo preview', html)
        undone = self.request(path + '/reply', {'msg': 'UNDO'})
        self.assertIn('Put back', undone)
        self.assertNotIn('Local simulator only', undone)

    def test_missing_or_invalid_messaging_setup_keeps_demo_available(self):
        with patch.object(self.app.messaging, 'configuration', return_value={'configured': False, 'missing': ['key']}):
            service, cfg = self.app.initialize_messaging()
        self.assertIsNone(service)
        self.assertEqual(cfg['mode'], 'demo')
        with patch.object(self.app.messaging, 'configuration', return_value={'configured': True, 'missing': []}), \
             patch.object(self.app.messaging, 'from_env', side_effect=ValueError('invalid setup')):
            service, cfg = self.app.initialize_messaging()
        self.assertIsNone(service)
        self.assertEqual(cfg['mode'], 'demo')
        self.assertIn('demo ready', self.request('/'))

    def test_reset_does_not_resend_already_saved_hours(self):
        path = '/w/FI-D0017/christmas'
        self.request(path)
        self.request(path, {'a': 'yes'})
        html = self.request(path + '/reset')
        self.assertIn('No update needed', html)
        self.assertNotIn('Yes, update', html)
        self.assertEqual(self.s5.snapshot('FI-D0017', ['2026-12-24'])['2026-12-24'], 'closed')
        self.assertNotIn('christmas', [b[0] for b in self.app.bundles_for('FI-D0017')])

    def test_only_missing_days_proposed_and_matching_regular_hours_still_need_exception(self):
        self.s5.apply({'entity_id': 'FI-D0017', 'suggestion_id': 'existing', 'actor': 'owner',
                       'items': [{'date': '2026-12-24', 'hours': 'closed', 'decision': 'ACCEPTED', 'seen_before': None}]})
        b = self.app.bundle('FI-D0017', 'christmas')
        self.assertEqual([s['day'].day for s in b[2]], [25, 26])
        payload = self.app.messaging_proposal('FI-D0017', 'christmas')
        self.assertNotIn('2026-12-24', [s['date'] for s in payload['items']])

    def test_proposal_preserved_and_intervening_write_not_overwritten(self):
        path = '/w/FI-D0017/christmas'
        original = self.request(path)
        self.s5.apply({'entity_id': 'FI-D0017', 'suggestion_id': 'elsewhere', 'actor': 'owner',
                       'items': [{'date': '2026-12-24', 'hours': '10:00-15:00', 'decision': 'ACCEPTED', 'seen_before': None}]})
        self.assertEqual(original, self.request(path))
        result = self.request(path, {'a': 'yes'})
        self.assertIn("didn&#x27;t change it", result)
        self.assertEqual(self.s5.snapshot('FI-D0017', ['2026-12-24'])['2026-12-24'], '10:00-15:00')

    def test_unclear_reply_never_partly_applies_or_keeps_old_draft(self):
        path = '/w/FI-D0017/christmas'
        self.request(path, {'a': 'other'})
        self.request(path + '/reply', {'msg': '24 Dec 10-14'})
        for text in ('24 Dec 10-14; 25 Dec maybe', '24 Dec 10:99-14:00',
                     '24 Dec not closed', '24 Dec 10-14; 24 Dec closed'):
            html = self.request(path + '/reply', {'msg': text})
            self.assertIn('Nothing has been saved', html)
            self.assertNotIn(('w', 'FI-D0017', 'christmas'), self.app.PENDING_HOURS)
        self.request(path + '/reply', {'msg': 'CONFIRM'})
        self.assertIsNone(self.s5.snapshot('FI-D0017', ['2026-12-24'])['2026-12-24'])

    def test_ai_reply_is_used_and_confirmed_before_apply(self):
        path = '/w/FI-D0017/christmas'
        self.request(path, {'a': 'other'})
        with patch.object(self.app.hours_ai, 'configured', return_value=True), \
             patch.object(self.app.hours_ai, 'read_reply', return_value=({date(2026,12,24): '12:00-18:00'}, [])) as ai:
            self.request(path + '/reply', {'msg': 'On Christmas Eve we open at noon and close at 6pm'})
        ai.assert_called_once()
        self.assertIsNone(self.s5.snapshot('FI-D0017', ['2026-12-24'])['2026-12-24'])
        self.request(path + '/reply', {'msg': 'CONFIRM'})
        self.assertEqual(self.s5.snapshot('FI-D0017', ['2026-12-24'])['2026-12-24'], '12:00-18:00')

    def test_ai_failure_never_falls_back_and_cancel_does_not_write(self):
        path = '/w/FI-D0017/christmas'
        self.request(path, {'a': 'other'})
        with patch.object(self.app.hours_ai, 'configured', return_value=True), \
             patch.object(self.app.hours_ai, 'read_reply', return_value=({}, ['unavailable'])):
            self.request(path + '/reply', {'msg': '24 Dec 10-14'})
        self.assertNotIn(('w', 'FI-D0017', 'christmas'), self.app.PENDING_HOURS)
        self.request(path + '/reply', {'msg': '24 Dec 10-14'})
        self.request(path + '/reply', {'msg': 'CANCEL'})
        self.request(path + '/reply', {'msg': 'CONFIRM'})
        self.assertIsNone(self.s5.snapshot('FI-D0017', ['2026-12-24'])['2026-12-24'])

    def test_direct_reply_and_plain_yes_work_without_quick_buttons(self):
        path = '/w/FI-D0017/christmas'
        html = self.request(path)
        self.assertIn('aria-label=\'Reply\'', html)
        self.assertIn('AI parsing not configured', html)
        self.request(path + '/reply', {'msg': '24 Dec 10-14'})
        self.assertIsNone(self.s5.snapshot('FI-D0017', ['2026-12-24'])['2026-12-24'])
        self.request(path + '/reply', {'msg': 'yes'})
        self.assertEqual(self.s5.snapshot('FI-D0017', ['2026-12-24'])['2026-12-24'], '10:00-14:00')

    def test_plain_yes_accepts_initial_suggestion(self):
        path = '/w/FI-D0017/christmas'
        self.request(path)
        self.request(path + '/reply', {'msg': 'yes'})
        self.assertEqual(self.s5.snapshot('FI-D0017', ['2026-12-24'])['2026-12-24'], 'closed')

    def test_live_read_failure_never_creates_a_proposal(self):
        with patch.object(self.s5, 'snapshot', side_effect=self.app.YextError(503, 'unavailable')):
            with self.assertRaises(urllib.error.HTTPError) as err:
                self.request('/w/FI-D0017/christmas')
        self.assertEqual(err.exception.code, 503)
        self.assertNotIn(('FI-D0017', 'christmas'), self.app.PROPOSALS)

    def test_single_day_apply_failure_is_not_presented_as_saved(self):
        failure = {'items': [{'date': '2026-12-24', 'status': 'FAILED', 'hours': 'closed'}],
                   'message': 'The update could not be verified.', 'applied': []}
        with patch.object(self.app, 'apply_via_step5', return_value=failure):
            html = self.request('/m/FI-D0017/2026-12-24/approve', {})
        self.assertIn('No saved changes confirmed', html)
        self.assertIn('The update could not be verified', html)
        self.assertNotIn('hours saved', html)
        self.assertEqual(self.app.DECISIONS[('FI-D0017', '2026-12-24')], 'Not saved; review required')

    def test_bundle_confirmation_only_lists_verified_days_as_saved(self):
        b = self.app.bundle('FI-D0017', 'christmas')
        items = b[2]
        failure = {'items': [{'date': x['day'].isoformat(), 'status': 'CONFLICT', 'hours': x['hours']} for x in items],
                   'message': 'Existing changes were kept.', 'applied': []}
        html = self.app.bundle_done('FI-D0017', b[1], [(x, x['hours']) for x in items], False, failure)
        self.assertIn('No saved changes confirmed', html)
        self.assertNotIn('hours saved', html)

    def test_unconfigured_real_webhook_fails_closed(self):
        with patch.object(self.app, 'MESSAGING', None):
            with self.assertRaises(urllib.error.HTTPError) as err:
                self.request('/messaging/inbound', {'Body': 'YES deadbeef'})
        self.assertEqual(err.exception.code, 503)

    def test_hero_real_template_fits_provider_limit(self):
        for eid in ('FI-D0001', 'FI-D0017'):
            with patch.object(self.app, 'instagram_days', return_value=set()):
                payload = self.app.messaging_proposal(eid, 'christmas')
            self.assertEqual(self.app.messaging.template_variables(payload, 'deadbeef')['4'], 'deadbeef')

if __name__=='__main__':unittest.main()
