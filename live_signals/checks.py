"""Live-event hours checks. Select questions worth asking; never invent opening hours.

Selection is an explicit, conservative demo policy, not a calibrated demand model.
The existing owner flow owns contact preferences, decisions and all listing writes.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from step5_apply import hours as H
from .context import LiveContext
from .datasets import DATA
from .providers import distance_km

SEGMENTS = {'cafe', 'bakery', 'restaurant', 'pizzeria', 'bar_pub'}
EXCLUDE = re.compile(r'\b(nursery|babies|baby|storytime|lorutuokio|satutuokio|vauva\w*|pienten|platinum|parking|vip|package)\b', re.I)
RELEVANT = re.compile(r'\b(music|concert\w*|konsert\w*|festival\w*|sports?|icehockey|jääkiekko|football|live music)\b', re.I)


def load_businesses():
    return {e['meta']['id']: e for e in json.loads((DATA/'entities.json').read_text(encoding='utf-8'))['response']['entities']}


def listed_hours(entity, day):
    return H.item_to_str(entity['hours'].get(H.DAYS[day.weekday()])) or 'closed'


def closing_minutes(hours):
    if hours in ('closed', 'regular') or H.validate(hours):
        return None
    ends = []
    for interval in hours.split(','):
        a, b = interval.split('-')
        start, end = (int(x[:2])*60+int(x[3:]) for x in (a, b))
        ends.append(end + (1440 if end <= start else 0))
    return max(ends)


def search_history(entity_ids, snapshot=date(2026, 11, 6)):
    """Observed weekday averages only; never describe these as event attendance/forecast.

    Optional Arrow reader avoids loading the 3-million-row table into the owner app.
    Without it, proximity provides deterministic ordering and analytics stays unknown.
    """
    try:
        import pyarrow as pa
        import pyarrow.compute as pc
        import pyarrow.parquet as pq
    except ImportError:
        return {}, 'unavailable: install pyarrow to rank by observed business search activity'
    sums, counts = defaultdict(float), Counter()
    wanted = pa.array(sorted(entity_ids), type=pa.large_string())
    columns = ['entity_id', 'date', 'GOOGLE_SEARCH_VIEWS']
    for batch in pq.ParquetFile(DATA/'analytics_daily.parquet').iter_batches(columns=columns):
        dates = batch.column('date')
        mask = pc.and_(pc.is_in(batch.column('entity_id'), value_set=wanted),
                       pc.and_(pc.greater_equal(dates, pa.scalar(snapshot-timedelta(days=56))),
                               pc.less(dates, pa.scalar(snapshot-timedelta(days=2)))))
        for r in batch.filter(mask).to_pylist():
            if r['GOOGLE_SEARCH_VIEWS'] is not None:
                key = (r['entity_id'], r['date'].weekday())
                sums[key] += r['GOOGLE_SEARCH_VIEWS']; counts[key] += 1
    return {k: round(v/counts[k], 2) for k, v in sums.items()}, 'observed public search views, 8-week window with 2-day availability lag'


def select_checks(context, entities, contacts=None, decisions=None, analytics=None, limit=3):
    """Return one question per business, spread across countries before filling slots.

    Matching requires a relevant timed performance, <=750 m, >=24 h lead time,
    and a start within 30 min before to 90 min after the business's normal close.
    Those thresholds are a reviewable hypothesis; they do not estimate demand.
    """
    contacts, decisions, analytics = contacts or {}, decisions or {}, analytics or {}
    flagged = {eid for (eid, _), row in decisions.items() if row.get('action') == 'FLAG'}
    rejected, candidates = Counter(), []
    for e in context.events.values():
        description = ' '.join([e['name']['en'], *e.get('keywords', [])])
        if EXCLUDE.search(description) or not RELEVANT.search(description):
            rejected['event_not_relevant'] += 1; continue
        stamp = datetime.fromisoformat(e['start_time'])
        if stamp-context.as_of < timedelta(hours=24):
            rejected['too_late_for_a_useful_check'] += 1; continue
        day = stamp.date(); start = stamp.hour*60+stamp.minute
        if not e['location'].get('name', {}).get('en'):
            rejected['venue_unknown'] += 1; continue
        for eid, entity in entities.items():
            if eid in flagged or entity.get('c_segment') not in SEGMENTS or entity.get('closed'):
                continue
            if str(contacts.get(eid, {}).get('opted_out_of_suggestions', '0')) == '1':
                continue
            if (eid, day.isoformat()) in decisions:  # Existing C13 SUGGEST/FLAG takes precedence.
                continue
            if any(r.get('date') == day.isoformat() for r in entity['hours'].get('holidayHours', [])):
                continue
            point = entity.get('geocodedCoordinate', {})
            if not point or e['x_country'] != entity['meta']['countryCode']:
                continue
            lon, lat = e['location']['position']['coordinates']
            distance = distance_km(point['latitude'], point['longitude'], lat, lon)
            if distance > .75:
                continue
            hours = listed_hours(entity, day); close = closing_minutes(hours)
            if close is None or not -30 <= start-close <= 90:
                rejected['no_closing_time_question'] += 1; continue
            target = {'date': day.isoformat(), 'event_id': e['@id']}
            if not context.event_for(target, entity):
                rejected['unavailable_or_cancelled'] += 1; continue
            identifier = 'live-' + hashlib.sha256((eid+'|'+e['@id']).encode()).hexdigest()[:16]
            candidates.append({'id': identifier, 'entity_id': eid, 'date': day.isoformat(),
                'kind': 'CHECK_HOURS', 'published_hours': hours, 'proposed_hours': None,
                'event_id': e['@id'], 'event': e, 'distance_m': round(distance*1000/10)*10,
                'country': e['x_country'], 'business_segment': entity['c_segment'],
                'observed_weekday_search_views': analytics.get((eid, day.weekday())),
                'weather': context.weather_for(entity['address']['city'], day.isoformat()) or None,
                'reason': 'Relevant performance near normal closing time; ask the owner whether the listed hours are right.',
                'confidence': None})
    candidates.sort(key=lambda r: (-(r['observed_weekday_search_views'] or 0), r['distance_m'], r['date'], r['id']))
    unique = {}; selected = []
    for r in candidates:
        unique.setdefault(r['entity_id'], r)
    pool = list(unique.values())
    for country in ('FI', 'SE', 'NO'):
        match = next((r for r in pool if r['country'] == country), None)
        if match and len(selected) < limit:
            selected.append(match)
    for r in pool:
        if len(selected) >= limit: break
        if r not in selected: selected.append(r)
    return selected, {'eligible_matches': len(candidates), 'eligible_businesses': len(unique),
                      'selected_checks': len(selected), 'excluded': dict(rejected)}


def build_checks(bundle, as_of=None, limit=3, with_analytics=True):
    if not 1 <= limit <= 20:
        raise ValueError('Use 1–20 live checks for the demo')
    context = LiveContext(bundle, as_of=as_of)
    entities = load_businesses()
    with (DATA/'customer_contacts.csv').open(encoding='utf-8', newline='') as f:
        contacts = {r['entity_id']: r for r in csv.DictReader(f)}
    # Block an event check whenever the chosen engine already has an action for that business/date.
    source = DATA.parent.parent/'engine/output/predictions_2026.csv'
    if not source.exists(): raise ValueError('Generate the C13 export before selecting live checks')
    with source.open(encoding='utf-8', newline='') as f:
        decisions = {(r['entity_id'], r['date']): r for r in csv.DictReader(f) if r['action'] != 'NO_ACTION'}
    preliminary, _ = select_checks(context, entities, contacts, decisions, limit=len(entities))
    analytics, analytics_status = search_history({r['entity_id'] for r in preliminary}) if with_analytics else ({}, 'not requested')
    checks, report = select_checks(context, entities, contacts, decisions, analytics, limit)
    return {'schema_version': 1, 'mode': 'recorded_replay' if as_of else 'live', 'as_of': context.as_of.isoformat(),
            'bundle': str(Path(bundle).resolve()), 'checks': checks, 'report': report,
            'analytics': analytics_status,
            'policy': 'One check per business per run; current listing only; no inferred hours, attendance or demand probability.',
            'scope': 'Real public events and forecasts, synthetic challenge businesses and historical analytics. Not benchmark predictions.'}
