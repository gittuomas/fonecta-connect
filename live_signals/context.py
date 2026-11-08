"""Read-only source replacement consumed by ContextFeatureStore, explicitly opt-in."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

from .datasets import read_csv
from .providers import distance_km, utcnow


class LiveContext:
    # The event listings have no demonstrated demand model or reliable attendance.
    # They supply evidence/features, never a new schedule or an extension candidate.
    allows_event_candidates = False

    def __init__(self, directory, as_of=None):
        self.directory = Path(directory)
        self.as_of = as_of or utcnow()
        if self.as_of.tzinfo is None:
            raise ValueError('as_of must include a timezone')
        self.manifest = json.loads((self.directory / 'manifest.json').read_text(encoding='utf-8'))
        if self.manifest.get('schema_version') != 1 or not self.manifest.get('research_only'):
            raise ValueError('Expected a live-context research bundle')
        if datetime.fromisoformat(self.manifest['generated_at']) > self.as_of:
            raise ValueError('Bundle was collected after as_of; it cannot be used for that historical decision')
        for name in ('local_events.json', 'weather_daily.csv'):
            if hashlib.sha256((self.directory / name).read_bytes()).hexdigest() != self.manifest['files'].get(name):
                raise ValueError('Context bundle checksum mismatch; finish or repeat the refresh')
        self.events = {e['@id']: e for e in json.loads((self.directory / 'local_events.json').read_text(encoding='utf-8'))['data']}
        self.forecasts = {(r['city'], r['date']): r for r in read_csv(self.directory / 'weather_daily.csv')}
        self.weather_sources = {r['city']: r for r in self.manifest['weather_sources']}

    def fresh(self, state, stamp):
        if state not in ('live', 'cached') or not stamp:
            return False
        age = self.as_of - datetime.fromisoformat(stamp)
        return timedelta(0) <= age <= timedelta(hours=1)

    def weather_for(self, city, day):
        source = self.weather_sources.get(city, {})
        if not self.fresh(source.get('status'), source.get('fetched_at')):
            return {}
        row = self.forecasts.get((city, day), {})
        return row if row.get('tmean_c') not in ('', None) else {}

    def event_for(self, target, entity):
        event_id = target.get('event_id')
        # Synthetic target IDs describe different events; never relabel them as real listings.
        if event_id and event_id not in self.events:
            return {}
        possible = [self.events[event_id]] if event_id else self.events.values()
        matches = []
        point = entity.get('geocodedCoordinate', {})
        if not point:
            return {}
        for e in possible:
            provenance = e['x_provenance']
            if (e['x_country'] != entity['meta']['countryCode'] or e['start_time'][:10] != target['date']
                    or e['event_status'] not in ('scheduled', 'rescheduled')
                    or not self.fresh(provenance.get('source_status'), provenance.get('fetched_at'))):
                continue
            if datetime.fromisoformat(e['start_time']) <= self.as_of:
                continue
            lon, lat = e['location']['position']['coordinates']
            distance = distance_km(point['latitude'], point['longitude'], lat, lon)
            if distance <= 1:
                matches.append((distance, e['@id'], e))
        # This is context selection, not a business-importance score.
        return min(matches, key=lambda row: row[:2])[2] if matches else {}

    def evidence(self, target, entity):
        e = self.event_for(target, entity)
        forecast = self.weather_for(entity['address']['city'], target['date'])
        return {'as_of': self.as_of.isoformat(), 'event': e or None, 'weather': forecast or None,
                'event_hours_candidates_enabled': False,
                'reason': 'Event proximity is context only; attendance, incremental demand and owner intent are unverified.',
                'selected_model_uses_live_context': False}
