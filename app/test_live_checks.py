"""Live facts -> existing owner decisions -> local Apply/readback/UNDO, over HTTP."""
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import test_predictions as baseline_tests
from live_signals.owner import CheckFeed
from live_signals.test_checks import fixture
from live_signals.datasets import write_bundle


class LiveOwnerFlow(unittest.TestCase):
    setUpClass = classmethod(baseline_tests.OwnerFlow.setUpClass.__func__)
    tearDownClass = classmethod(baseline_tests.OwnerFlow.tearDownClass.__func__)
    request = baseline_tests.OwnerFlow.request

    def setUp(self):
        baseline_tests.OwnerFlow.setUp(self)
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.eid = 'FI-00762'
        context, ent, path = fixture(tmp.name, self.app.BL.ents[self.eid], self.eid)
        self.context = context
        self.app.LIVE = CheckFeed(path, state_path=Path(tmp.name)/'seen.json')
        self.pid = next(iter(self.app.LIVE.rows))[1]
        self.path = f'/w/{self.eid}/{self.pid}'
        self.web = f'/b/{self.eid}/{self.pid}'
        self.day = '2026-11-14'

    def test_check_preserves_hours_and_uses_existing_controls(self):
        html = self.request(self.path)
        for text in ('Test concert', 'near your closing time', 'Your listed hours: 10–18',
                     'Yes, confirm hours', 'Not relevant', 'Other hours'):
            self.assertIn(text, html)
        self.assertNotIn('We suggest:', html)
        self.assertNotIn('Last year', html)
        self.assertEqual(self.s5.mock.module.STORE.update_log, [])
        self.assertIn('Live hours checks:', self.request('/'))

    def test_confirm_repeat_and_undo(self):
        self.request(self.path)
        self.request(self.path, {'a': 'yes'})
        self.assertEqual(self.s5.snapshot(self.eid, [self.day])[self.day], '10:00-18:00')
        self.assertNotIn(('w', self.eid, self.pid), self.app.WA_AUTO)
        n = len(self.s5.mock.module.STORE.update_log)
        self.request(self.path, {'a': 'yes'})
        self.assertEqual(len(self.s5.mock.module.STORE.update_log), n)
        self.request(self.path+'/reply', {'msg': 'UNDO'})
        self.assertIsNone(self.s5.snapshot(self.eid, [self.day])[self.day])
        self.assertTrue(any(r['type'] == 'LIVE_CHECK_DECISION' for r in self.s5.audit(self.eid)))

    def test_owner_supplies_different_hours(self):
        self.request(self.path)
        self.request(self.path, {'a': 'other'})
        self.request(self.path+'/reply', {'msg': '14 Nov 10-20'})
        self.assertIsNone(self.s5.snapshot(self.eid, [self.day])[self.day])
        self.request(self.path+'/reply', {'msg': 'CONFIRM'})
        self.assertEqual(self.s5.snapshot(self.eid, [self.day])[self.day], '10:00-20:00')
        self.assertEqual(self.app.LEARN.owner(self.eid)['history'][-1]['outcome'], 'EDIT')

    def test_decline_has_no_write_and_learns_event_preference(self):
        self.request(self.path, {'a': 'no'})
        self.assertEqual(self.s5.mock.module.STORE.update_log, [])
        self.request(self.path+'/more', {'a': 'no'})
        self.assertFalse(self.app.LEARN.reason_on(self.eid, 'event'))
        self.app.WA.clear()  # reopening after a process restart cannot create a new message
        self.assertIsNone(self.app.bundle(self.eid, self.pid))

    def test_stop_quiet_mode_and_stale_source_block_unopened_checks(self):
        self.app.LEARN.set_stopped(self.eid, True)
        self.assertIsNone(self.app.bundle(self.eid, self.pid))
        self.app.LEARN.reset()
        for _ in range(3): self.app.LEARN.record(self.eid, [], 'NO_REPLY', 'old')
        self.assertIsNone(self.app.bundle(self.eid, self.pid))
        self.app.LEARN.reset()
        with patch.object(self.app.LIVE, 'unavailable', return_value='Source expired'):
            with self.assertRaises(urllib.error.HTTPError) as err: self.request(self.path)
            self.assertEqual(err.exception.code, 404)

    def test_web_confirmation_never_grants_annual_permission(self):
        html = self.request(self.web)
        self.assertIn('Confirm listed hours', html)
        self.assertNotIn("name='always'", html)
        self.request(self.web+'/approve', {'always': '1'})
        self.assertEqual(self.s5.snapshot(self.eid, [self.day])[self.day], '10:00-18:00')
        self.assertFalse(any(r['type'] == 'PERMISSION_GRANTED' for r in self.s5.audit(self.eid)))
        self.assertEqual(self.app.LEARN.owner(self.eid)['history'][-1]['outcome'], 'YES')
        self.assertIsNone(self.app.bundle(self.eid, self.pid))

    def test_no_reminder_and_all_laura_channels_show_confirmation_hours(self):
        self.request(self.path+'/remind', {})
        self.assertNotIn(('w', self.eid, self.pid), self.app.REMIND)
        for channel in ('s', 'e', 'p'):
            text = self.request(f'/{channel}/{self.eid}/{self.pid}')
            self.assertIn('listed hours', text.lower())
            self.assertNotIn('We suggest:', text)
            self.assertIn('Test concert', text)

    def test_sms_approval_and_undo_use_step5(self):
        path = f'/s/{self.eid}/{self.pid}'
        self.request(path)
        self.request(path+'/reply', {'msg': 'YES'})
        self.assertEqual(self.s5.snapshot(self.eid, [self.day])[self.day], '10:00-18:00')
        self.request(path+'/reply', {'msg': 'UNDO'})
        self.assertIsNone(self.s5.snapshot(self.eid, [self.day])[self.day])

    def test_email_scanner_get_never_writes_and_stop_learns_event_preference(self):
        path = f'/e/{self.eid}/{self.pid}'
        self.request(path+'/yes')
        self.assertEqual(self.s5.mock.module.STORE.update_log, [])
        self.request(path+'/stop')
        self.request(path, {'a': 'stop'})
        self.assertFalse(self.app.LEARN.reason_on(self.eid, 'event'))
        self.assertEqual(self.s5.mock.module.STORE.update_log, [])

    def test_email_confirmation_and_push_undo_use_existing_handlers(self):
        for channel in ('e', 'p'):
            with self.subTest(channel=channel):
                self.setUp()
                path = f'/{channel}/{self.eid}/{self.pid}'
                self.request(path)
                self.request(path, {'a': 'yes'})
                self.assertEqual(self.s5.snapshot(self.eid, [self.day])[self.day], '10:00-18:00')
                if channel == 'p':
                    self.request(path, {'a': 'undo'})
                    self.assertIsNone(self.s5.snapshot(self.eid, [self.day])[self.day])

    def test_real_mirror_result_reaches_operator_without_rewriting_owner_message(self):
        from step5_apply.yext import YextClient
        self.s5.applier.sandbox = YextClient(self.s5.mock.url, 'demo', name='sandbox')
        self.s5.applier.mirror_map = {self.eid: 'FI-D0003'}
        self.addCleanup(setattr, self.s5.applier, 'sandbox', None)
        self.addCleanup(setattr, self.s5.applier, 'mirror_map', {})
        self.request(self.path, {'a': 'yes'})
        self.assertIn('Yext sandbox write verified', self.request('/'))
        self.assertEqual(self.s5.snapshot('FI-D0003', [self.day])[self.day], '10:00-18:00')
        self.request(self.path+'/reply', {'msg': 'UNDO'})
        self.assertIn('Yext sandbox undo verified', self.request('/'))
        self.assertIsNone(self.s5.snapshot('FI-D0003', [self.day])[self.day])

    def test_weather_replacement_is_read_in_existing_panel(self):
        meta = self.context.manifest.copy()
        meta['weather_sources'] = [{'city': 'Helsinki', 'status': 'live', 'fetched_at': meta['generated_at']}]
        weather = [{'city': 'Helsinki', 'country': 'FI', 'date': self.day, 'tmean_c': 3.3,
                    'tmin_c': 1, 'tmax_c': 5, 'precip_mm': 2, 'snow_depth_cm': None, 'is_forecast': 1}]
        write_bundle(self.context.directory, list(self.context.events.values()), weather, meta)
        html = self.request('/')
        self.assertIn('Open-Meteo forecast', html)
        self.assertIn('mean 3.3', html)
        self.assertNotIn('snow depth 0', html)
        self.assertNotIn('Open-Meteo', self.request(self.path))

    def weather_bundle(self):
        """Real replacement format, with controlled weather for Laura's ski case."""
        from datetime import date
        eid = 'FI-D0005'
        city = self.app.BL.ents[eid]['address']['city']
        day = '2026-11-09'
        self.assertNotIn((self.app.engine_row(eid, date.fromisoformat(day)) or {}).get('action'),
                         ('SUGGEST', 'FLAG'))
        meta = self.context.manifest.copy()
        meta['weather_sources'] = [{'city': city, 'status': 'live', 'fetched_at': meta['generated_at']}]
        weather = [{'city': city, 'country': 'FI', 'date': day, 'tmean_c': -5,
                    'tmin_c': -7, 'tmax_c': -3, 'precip_mm': 12, 'snow_depth_cm': 35,
                    'snowfall_cm': 12, 'is_forecast': 1}]
        write_bundle(self.context.directory, list(self.context.events.values()), weather, meta)
        return eid, day, f'weather-{day}'

    def test_live_weather_enters_laura_flow_and_keeps_reply_after_feedback(self):
        eid, day, pid = self.weather_bundle()
        path = f'/w/{eid}/{pid}'
        with patch.object(self.app.proactive, 'season_check', return_value=None), \
             patch.object(self.app.proactive, '_weather', side_effect=AssertionError('no fixture fallback')):
            html = self.request(path)
            self.assertIn('12 cm of new snow', html)
            self.assertIn('Will you be open as usual?', html)
            self.assertIn('Open-Meteo live forecast', self.request('/'))
            self.request(path, {'a': 'no'})
            self.request(path+'/more', {'a': 'no'})
            self.assertFalse(self.app.LEARN.reason_on(eid, 'weather'))
            self.assertEqual(self.s5.mock.module.STORE.update_log, [])
            self.assertIn('Fonecta', self.request(path))

    def test_weather_web_approval_cannot_grant_recurring_permission(self):
        eid, day, pid = self.weather_bundle()
        path = f'/b/{eid}/{pid}'
        self.assertNotIn("name='always'", self.request(path))
        self.request(path+'/approve', {'always': '1'})
        self.assertIsNotNone(self.s5.snapshot(eid, [day])[day])
        records = self.s5.audit(eid)
        self.assertFalse(any(r['type'] == 'PERMISSION_GRANTED' for r in records))
        self.assertEqual(self.app.LEARN.owner(eid)['history'][-1]['reasons'], ['weather'])

    def test_live_weather_expiry_and_c13_action_suppress_check(self):
        eid, day, pid = self.weather_bundle()
        original = self.app.engine_row
        def engine_row(business, dt):
            return {'action': 'SUGGEST'} if business == eid and dt.isoformat() == day else original(business, dt)
        with patch.object(self.app, 'engine_row', side_effect=engine_row):
            self.assertNotIn(pid, [b[0] for b in self.app.bundles_for(eid)])
        with patch.object(self.app.LIVE, 'weather_for_city', return_value={}):
            self.assertNotIn(pid, [b[0] for b in self.app.bundles_for(eid)])

    def test_owner_exception_supersedes_unopened_check(self):
        self.s5.apply({'entity_id': self.eid, 'suggestion_id': 'owner-own-change',
                       'items': [{'date': self.day, 'decision': 'EDITED', 'hours': 'closed'}]})
        self.assertIsNone(self.app.bundle(self.eid, self.pid))

    def test_live_check_on_holiday_cannot_become_annual_permission(self):
        item = self.app.bundle(self.eid, self.pid)[2][0]
        from datetime import date
        item = dict(item, day=date(2026, 12, 24))
        self.assertEqual(self.app.auto_grants(self.eid, [item],
            {'items': [{'date': '2026-12-24', 'status': 'APPLIED'}]}), [])


if __name__ == '__main__': unittest.main()
