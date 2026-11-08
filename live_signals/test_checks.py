"""Decision-policy regressions using controlled source facts, no network calls."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .checks import select_checks
from .context import LiveContext
from .datasets import write_bundle, event_record
from .owner import CheckFeed
from .providers import event

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def fixture(directory, entity=None, eid='TEST'):
    entity = copy.deepcopy(entity) if entity else {
        'meta': {'countryCode': 'FI'}, 'name': 'Test cafe', 'c_segment': 'cafe',
        'address': {'city': 'Helsinki'}, 'geocodedCoordinate': {'latitude': 60.17, 'longitude': 24.93},
        'hours': {'saturday': {'openIntervals': [{'start': '10:00', 'end': '18:00'}]}}}
    point = entity['geocodedCoordinate']
    raw = event('helsinki', 'test-concert', 'FI', 'Test concert', 'Concert hall',
                point['latitude'], point['longitude'], '2026-11-14T19:00:00+02:00', None,
                'https://example.com/concert', 'music', fetched_at=NOW.isoformat())
    row = event_record(dict(raw, source_status='live'))
    meta = {'generated_at': NOW.isoformat(), 'weather_sources': []}
    write_bundle(directory, [row], [], meta)
    context = LiveContext(directory, as_of=NOW)
    checks, report = select_checks(context, {eid: entity})
    path = Path(directory)/'checks.json'
    path.write_text(json.dumps({'schema_version': 1, 'mode': 'recorded_replay', 'as_of': NOW.isoformat(),
                               'bundle': str(directory), 'checks': checks, 'report': report}), encoding='utf-8')
    return context, entity, path


class CheckPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.context, self.entity, self.path = fixture(self.tmp.name)
        self.event = next(iter(self.context.events.values()))

    def select(self, **kwargs):
        return select_checks(self.context, {'TEST': self.entity}, **kwargs)[0]

    def test_relevant_closing_time_question_never_invents_hours(self):
        row = self.select()[0]
        self.assertEqual(row['kind'], 'CHECK_HOURS')
        self.assertEqual(row['published_hours'], '10:00-18:00')
        self.assertIsNone(row['proposed_hours'])
        self.assertIsNone(row['observed_weekday_search_views'])
        self.assertIsNone(row['event']['end_time'])

    def test_nursery_rhymes_and_ticket_addons_stay_quiet_even_near_close(self):
        for title in ('Pienten lorutuokio', 'Nursery music', 'Concert VIP package', 'Concert parking'):
            self.event['name']['en'] = title
            with self.subTest(title=title): self.assertEqual(self.select(), [])

    def test_event_during_normal_day_is_not_a_closing_time_question(self):
        self.event['start_time'] = '2026-11-14T09:30:00+02:00'
        self.assertEqual(self.select(), [])

    def test_stale_cancelled_and_late_checks_stay_quiet(self):
        self.context.as_of = NOW + timedelta(hours=2)
        self.assertEqual(self.select(), [])
        self.context.as_of = NOW
        self.event['event_status'] = 'cancelled'
        self.assertEqual(self.select(), [])
        self.event['event_status'] = 'scheduled'
        self.event['start_time'] = '2026-11-07T19:00:00+02:00'
        self.assertEqual(self.select(), [])

    def test_c13_decisions_contact_optout_and_existing_exceptions_win(self):
        for decisions in ({('TEST', '2026-11-14'): {'action': 'SUGGEST'}},
                          {('TEST', '2026-12-24'): {'action': 'FLAG'}}):
            self.assertEqual(self.select(decisions=decisions), [])
        self.assertEqual(self.select(contacts={'TEST': {'opted_out_of_suggestions': '1'}}), [])
        self.entity['hours']['holidayHours'] = [{'date': '2026-11-14', 'isClosed': True}]
        self.assertEqual(self.select(), [])

    def test_wrong_country_distance_and_business_type_are_excluded(self):
        for change in ({'c_segment': 'plumber'}, {'meta': {'countryCode': 'SE'}},
                       {'geocodedCoordinate': {'latitude': 61, 'longitude': 25}}):
            ent = dict(self.entity, **change)
            self.assertEqual(select_checks(self.context, {'TEST': ent})[0], [])

    def test_one_check_per_business_and_country_spread(self):
        entities = {}
        for i, country in enumerate(('FI', 'SE', 'NO')):
            for j in range(2):
                ent = copy.deepcopy(self.entity); ent['meta']['countryCode'] = country
                entities[f'{country}-{j}'] = ent
            e = copy.deepcopy(self.event); e['@id'] = f'live:test-{i}'; e['x_country'] = country
            self.context.events[e['@id']] = e
        rows, _ = select_checks(self.context, entities, limit=3)
        self.assertEqual({r['country'] for r in rows}, {'FI', 'SE', 'NO'})
        self.assertEqual(len({r['entity_id'] for r in rows}), 3)

    def test_feed_revalidates_source_and_replay_cannot_target_sandbox(self):
        feed = CheckFeed(self.path)
        row = next(iter(feed.rows.values()))
        self.assertFalse(feed.unavailable(row, self.entity))
        with self.assertRaisesRegex(ValueError, 'local mock'): CheckFeed(self.path, sandbox=True)
        self.event['event_status'] = 'cancelled'
        write_bundle(self.tmp.name, [self.event], [], {'generated_at': NOW.isoformat(), 'weather_sources': []})
        self.assertTrue(feed.unavailable(row, self.entity))

    def test_cooldown_survives_reload_and_expires(self):
        state = Path(self.tmp.name)/'seen.json'
        feed = CheckFeed(self.path, state_path=state)
        feed.presented('TEST', 'live-a')
        feed = CheckFeed(self.path, state_path=state)
        self.assertFalse(feed.cooldown('TEST', 'live-a'))  # the owner can revisit the same message
        self.assertTrue(feed.cooldown('TEST', 'live-b'))
        feed.as_of += timedelta(days=14)
        self.assertFalse(feed.cooldown('TEST', 'live-b'))


if __name__ == '__main__': unittest.main()
