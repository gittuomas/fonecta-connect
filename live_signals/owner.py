"""Optional live-check adapter for the existing owner flow. No listing writes."""
from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from .checks import listed_hours
from .context import LiveContext
from .providers import utcnow


class CheckFeed:
    def __init__(self, path=None, *, sandbox=False, state_path=None):
        self.rows = {}
        self.mode = 'disabled'
        self.lock = threading.Lock()
        self.state_path = Path(state_path) if state_path else None
        if not path:
            return
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if data.get('schema_version') != 1 or data.get('mode') not in ('live', 'recorded_replay'):
            raise ValueError('Expected a live hours-check export')
        self.mode = data['mode']
        if sandbox and self.mode == 'recorded_replay':
            raise ValueError('Recorded live-check replays require the local mock; disable the Yext sandbox')
        self.as_of = datetime.fromisoformat(data['as_of'])
        if self.as_of.tzinfo is None:
            raise ValueError('Live checks require a timezone-aware cutoff')
        self.directory = Path(data['bundle'])
        for row in data['checks']:
            key = (row['entity_id'], row['id'])
            if (key in self.rows or not row['id'].startswith('live-') or row['kind'] != 'CHECK_HOURS'
                    or row['proposed_hours'] is not None or not row['event_id'].startswith('live:')):
                raise ValueError('Invalid or duplicate live check; hours must come from the owner')
            self.rows[key] = row

    def now(self):
        return self.as_of if self.mode == 'recorded_replay' else utcnow()

    def unavailable(self, row, entity):
        """Re-read the bundle: cancellation/removal, corruption and expiry fail closed."""
        try:
            context = LiveContext(self.directory, as_of=self.now())
            event = context.event_for({'date': row['date'], 'event_id': row['event_id']}, entity)
            if not event or event != row['event']:
                return 'Live evidence expired or changed; refresh the check'
            if listed_hours(entity, datetime.fromisoformat(row['date']).date()) != row['published_hours']:
                return 'Published schedule changed; refresh the check'
        except (OSError, ValueError, KeyError):
            return 'Live evidence unavailable; refresh the check'
        return ''

    def cooldown(self, eid, pid):
        if not self.state_path or not self.state_path.exists():
            return ''
        with self.lock:
            records = json.loads(self.state_path.read_text(encoding='utf-8'))
        for r in records.get(eid, []):
            if r['id'] != pid and self.now() - datetime.fromisoformat(r['at']) < timedelta(days=14):
                return 'One live event check per business in 14 days'
        return ''

    def weather_for(self, row, entity):
        try:
            context = LiveContext(self.directory, as_of=self.now())
            return context.weather_for(entity['address']['city'], row['date'])
        except (OSError, ValueError, KeyError):
            return {}

    def weather_for_city(self, city):
        """Replacement input for Laura's snow check. An empty live feed stays empty."""
        try:
            context = LiveContext(self.directory, as_of=self.now())
            return {date.fromisoformat(day): row for (name, day), row in context.forecasts.items()
                    if name == city and context.weather_for(city, day)}
        except (OSError, ValueError, KeyError):
            return {}

    def presented(self, eid, pid):
        if not self.state_path:
            return
        with self.lock:
            records = json.loads(self.state_path.read_text(encoding='utf-8')) if self.state_path.exists() else {}
            history = records.setdefault(eid, [])
            if any(r['id'] == pid for r in history):
                return
            history.append({'id': pid, 'at': self.now().isoformat()})
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(records), encoding='utf-8')
            tmp.replace(self.state_path)


def suggestion(row, entity):
    """Current hours are a confirmation choice, never a model-generated schedule."""
    day = datetime.fromisoformat(row['date']).date()
    event = row['event']
    title = event['name']['en']
    start = datetime.fromisoformat(event['start_time']).strftime('%H:%M')
    venue = event['location']['name']['en']
    reason = f"{title} starts at {start} at {venue}, about {row['distance_m']} metres away, near your closing time."
    return {'eid': row['entity_id'], 'name': entity['name'], 'city': entity['address']['city'],
            'lang': 'en', 'day': day, 'day_name': title, 'dm': f'{day.day} {day:%b}',
            'weekday': day.strftime('%A'), 'shown': row['published_hours'], 'hours': row['published_hours'],
            'action': 'CHECK_HOURS', 'basis': 'LIVE_CONTEXT', 'reason': reason, 'short': '', 'key': 'EVENT',
            'last_hours': None, 'event': event, 'contact_eligible': True, 'source': 'Live event check',
            'evidence': [row], 'confidence': None, 'intro': reason, 'shows': '', 'live_check': row}
