"""Provider and integration regressions. No external calls, labels or listing writes."""
import copy
import hashlib
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from .context import LiveContext
from .datasets import DATA, PACKAGE, daily_weather, event_record, refresh, write_bundle, write_json
from .providers import JsonCache, deduplicate, event, normalize_helsinki, normalize_stockholm, normalize_ticketmaster, parse_time, utcnow

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
DAY = date(2026, 11, 13)
CITY = {'city': 'Vantaa', 'country': 'FI', 'lat': '60.2934', 'lon': '25.0378'}


def activity():
    return dict(event('helsinki', 'rhyme', 'FI', 'Pienten lorutuokio', 'Library', 60.29, 25.03,
                      '2026-11-13T09:30:00+02:00', '2026-11-13T10:00:00+02:00', 'https://example.com/event',
                      'nursery rhyme babies music', fetched_at=NOW.isoformat()), source_status='live')


def weather_payload():
    return {'daily': {'time': [DAY.isoformat()], 'temperature_2m_mean': [4.5], 'temperature_2m_min': [1.0],
                      'temperature_2m_max': [7.0], 'precipitation_sum': [2.0]},
            'daily_units': {'temperature_2m_mean': '°C', 'temperature_2m_min': '°C', 'temperature_2m_max': '°C', 'precipitation_sum': 'mm'},
            'hourly': {'time': [DAY.isoformat()+'T12:00'], 'snow_depth': [0.05]}, 'hourly_units': {'snow_depth': 'm'}}


class Providers(unittest.TestCase):
    def test_stockholm_uses_occurrences_not_parent_ranges(self):
        row={'id':'s', 'title':{'en':'Show'}, 'url':'show', 'location':{'latitude':59.3,'longitude':18.1},
             'start_date':'2026-11-01','end_date':'2027-01-01', 'schedule':{'range':{'start':'2026-11-01'},
             'dates':[{'date':'2026-11-10','start_time':'23:00','end_time':'01:00'}, {'date':'2026-11-11'}]}}
        rows=list(normalize_stockholm([row], NOW.isoformat()))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['end'],'2026-11-11T01:00:00+01:00')

    def test_cancelled_and_unknown_helsinki_times(self):
        r={'id':'h','name':{'en':'Show'},'location':{'position':{'coordinates':[24.9,60.1]}},
           'start_time':'2026-11-10T17:00:00Z','end_time':'2026-11-10T19:00:00Z','event_status':'EventCancelled'}
        rows=list(normalize_helsinki([r,dict(r,super_event_type='recurring'),dict(r,has_start_time=False)], NOW.isoformat()))
        self.assertEqual(len(rows),1); self.assertEqual(rows[0]['status'],'cancelled')
        self.assertIsNone(list(normalize_helsinki([dict(r,has_end_time=False)],NOW.isoformat()))[0]['end'])

    def test_ticketmaster_missing_end_and_tba(self):
        r={'id':'t','name':'Show','dates':{'start':{'dateTime':'2026-11-10T17:00:00Z'},'status':{'code':'onsale'}},
           '_embedded':{'venues':[{'country':{'countryCode':'NO'},'location':{'latitude':'59.9','longitude':'10.7'}}]}}
        self.assertIsNone(list(normalize_ticketmaster([r],NOW.isoformat()))[0]['end'])
        r['dates']['start']['timeTBA']=True
        self.assertEqual(list(normalize_ticketmaster([r],NOW.isoformat())),[])

    def test_missing_times_and_nonfinite_coordinates(self):
        self.assertIsNone(parse_time('2026-11-13','FI'))
        self.assertIsNone(event('x','a','FI','Title','Place',float('nan'),24,'2026-11-10T10:00:00',None,''))

    def test_cancelled_duplicate_wins_in_both_orders(self):
        a=activity(); c=dict(a,status='cancelled',provider='other')
        for rows in ([a,c],[c,a]): self.assertEqual(deduplicate(rows)[0]['status'],'cancelled')

    def test_cached_evidence_is_not_relabelled_as_live(self):
        with tempfile.TemporaryDirectory() as d:
            url='https://example.com/events'; path=Path(d)/(hashlib.sha256(url.encode()).hexdigest()+'.json')
            write_json(path,{'fetched_at':(utcnow()-timedelta(days=2)).isoformat(),'data':[]})
            self.assertEqual(JsonCache(d,offline=True).get(url)[2],'stale')


class WeatherMapping(unittest.TestCase):
    def test_daily_snowfall_is_separate_from_snow_depth_and_checks_units(self):
        payload = weather_payload()
        payload['daily']['snowfall_sum'] = [12.4]
        payload['daily_units']['snowfall_sum'] = 'cm'
        row = daily_weather(payload, CITY, DAY, DAY+timedelta(days=1))[0]
        self.assertEqual(row['snowfall_cm'], 12.4)
        self.assertEqual(row['snow_depth_cm'], 5)
        payload['daily_units']['snowfall_sum'] = 'mm'
        self.assertIsNone(daily_weather(payload, CITY, DAY, DAY+timedelta(days=1))[0]['snowfall_cm'])

    def test_schema_units_and_local_date(self):
        r=daily_weather(weather_payload(),CITY,DAY,DAY+timedelta(days=1))[0]
        self.assertEqual((r['date'],r['tmean_c'],r['precip_mm'],r['snow_depth_cm']),('2026-11-13',4.5,2.0,5.0))
        self.assertEqual(r['is_forecast'],1)

    def test_unknown_is_not_zero_and_outside_horizon_is_not_filled(self):
        data=weather_payload(); data['hourly']['snow_depth']=[None]
        self.assertIsNone(daily_weather(data,CITY,DAY,DAY+timedelta(days=1))[0]['snow_depth_cm'])
        self.assertEqual(daily_weather(data,CITY,DAY+timedelta(days=1),DAY+timedelta(days=2)),[])
        data['hourly']['snow_depth']=[0]
        self.assertEqual(daily_weather(data,CITY,DAY,DAY+timedelta(days=1))[0]['snow_depth_cm'],0)

    def test_incorrect_units_are_not_assumed(self):
        data=weather_payload();data['hourly_units']['snow_depth']='cm';data['daily_units']['precipitation_sum']='inch'
        row=daily_weather(data,CITY,DAY,DAY+timedelta(days=1))[0]
        self.assertIsNone(row['snow_depth_cm']);self.assertIsNone(row['precip_mm'])


class ContextReader(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)
        self.e=event_record(activity())
        self.rows=daily_weather(weather_payload(),CITY,DAY,DAY+timedelta(days=1))
        self.meta={'generated_at':NOW.isoformat(), 'weather_sources':[{'city':'Vantaa','status':'live','fetched_at':NOW.isoformat()}]}
        self.entity={'meta':{'countryCode':'FI'},'address':{'city':'Vantaa'},
                     'geocodedCoordinate':{'latitude':60.29,'longitude':25.03}}
        self.target={'date':DAY.isoformat(),'event_id':self.e['@id']}
        self.save()

    def save(self):
        write_bundle(self.path,[self.e],self.rows,self.meta)

    def test_source_facts_do_not_propose_hours(self):
        context=LiveContext(self.path,as_of=NOW);evidence=context.evidence(self.target,self.entity)
        self.assertIsNone(evidence['event']['x_expected_attendance'])
        self.assertFalse(context.allows_event_candidates)
        self.assertFalse(evidence['selected_model_uses_live_context'])
        self.assertNotIn('message',evidence)
        self.assertEqual(float(evidence['weather']['snow_depth_cm']),5)

    def test_unknown_end_and_snow_remain_unknown(self):
        self.e['end_time']=None;self.rows[0]['snow_depth_cm']=None;self.save()
        context=LiveContext(self.path,as_of=NOW)
        self.assertIsNone(context.event_for(self.target,self.entity)['end_time'])
        self.assertEqual(context.weather_for('Vantaa',DAY.isoformat())['snow_depth_cm'],'')

    def test_synthetic_event_id_is_never_replaced_by_proximity(self):
        self.target['event_id']='synthetic:event:123'
        self.assertEqual(LiveContext(self.path,as_of=NOW).event_for(self.target,self.entity),{})

    def test_stale_cancelled_and_different_country_are_excluded(self):
        for key,value in [('event_status','cancelled'),('x_country','NO')]:
            saved=self.e[key];self.e[key]=value;self.save()
            self.assertEqual(LiveContext(self.path,as_of=NOW).event_for(self.target,self.entity),{})
            self.e[key]=saved
        self.save();context=LiveContext(self.path,as_of=NOW+timedelta(hours=2))
        self.assertEqual(context.event_for(self.target,self.entity),{})
        self.assertEqual(context.weather_for('Vantaa',DAY.isoformat()),{})

    def test_retrieval_cannot_leak_into_an_earlier_backtest(self):
        with self.assertRaisesRegex(ValueError,'collected after'):
            LiveContext(self.path,as_of=NOW-timedelta(days=1))
        with self.assertRaisesRegex(ValueError,'timezone'):
            LiveContext(self.path,as_of=NOW.replace(tzinfo=None))

    def test_changed_bundle_is_rejected(self):
        with (self.path/'weather_daily.csv').open('a') as f:f.write('changed')
        with self.assertRaisesRegex(ValueError,'checksum'):
            LiveContext(self.path,as_of=NOW)

    def test_refresh_cannot_overwrite_supplied_or_recorded_inputs(self):
        with patch('live_signals.datasets.fetch_events') as fetch:
            for destination in (DATA, DATA/'nested', DATA.parent, PACKAGE.parent, PACKAGE/'data/context_2026-11-07'):
                with self.subTest(destination=destination), self.assertRaisesRegex(ValueError,'read-only'):
                    refresh(output=destination)
            fetch.assert_not_called()

    def test_recorded_nursery_rhyme_is_evidence_only(self):
        from .__main__ import inspect_context
        bundle=Path(__file__).parent/'data/context_2026-11-07'
        meta=json.loads((bundle/'manifest.json').read_text(encoding='utf-8'))
        result=inspect_context(bundle,'FI-00925',DAY,datetime.fromisoformat(meta['generated_at']))
        from .datasets import DATA
        entity=next(e for e in json.loads((DATA/'entities.json').read_text(encoding='utf-8'))['response']['entities'] if e['meta']['id']=='FI-00925')
        context=LiveContext(bundle,as_of=datetime.fromisoformat(meta['generated_at']))
        matches=[context.event_for({'date':DAY.isoformat(),'event_id':e['@id']},entity)
                 for e in context.events.values() if e['name']['en']=='Pienten lorutuokio']
        self.assertTrue(any(matches),'The reported nursery-rhyme occurrence must remain available as evidence')
        self.assertIsNone(result['evidence']['event']['x_expected_attendance'])
        self.assertEqual(result['additional_candidate_hours'],{})
        self.assertFalse(result['evidence']['selected_model_uses_live_context'])


if __name__=='__main__':
    unittest.main()
