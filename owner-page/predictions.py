"""Read selected-model outputs and attach verifiable facts for existing messages.

No model fitting, labels, live-data heuristics, customer copy or external writes.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

from step5_apply import hours as H


def cycle(day):
    return day.year if day.month >= 2 else day.year - 1


class SelectedPredictions:
    def __init__(self, path, targets, entities, history, regular_history, calendar, snapshot='2026-11-06'):
        self.path = Path(path)
        self.targets, self.entities, self.regular_history = targets, entities, regular_history
        self.snapshot = snapshot
        raw = self.path.read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.rows = {}
        records = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(records, list):
            raise ValueError('Expected the engine demo_suggestions.json array')
        for r in records:
            required = {'entity_id', 'date', 'day_key', 'action', 'proposed_hours',
                        'shown_hours', 'contact_eligible', 'opted_out', 'model_id'}
            if not isinstance(r, dict) or not required <= r.keys():
                raise ValueError('Incomplete engine demo suggestion')
            key = (r['entity_id'], r['date'])
            if key in self.rows:
                raise ValueError(f'Duplicate prediction: {key}')
            if key not in targets:
                raise ValueError(f'Prediction is outside the official target set: {key}')
            action, hours = r['action'], r['proposed_hours'] or ''
            if r['day_key'] != targets[key]['day_key'] or not r['model_id']:
                raise ValueError(f'Engine metadata does not match target: {key}')
            if not isinstance(r['contact_eligible'], bool) or not isinstance(r['opted_out'], bool):
                raise ValueError(f'Invalid contact eligibility: {key}')
            if r['contact_eligible'] and (action != 'SUGGEST' or r['opted_out']):
                raise ValueError(f'Inconsistent contact eligibility: {key}')
            if H.validate(r['shown_hours']) or r['shown_hours'] == 'regular':
                raise ValueError(f'Invalid published hours: {key}')
            if action not in ('SUGGEST', 'NO_ACTION', 'FLAG'):
                raise ValueError(f'Unknown prediction action: {key}')
            if action == 'SUGGEST':
                if hours != H.normalise(hours) or hours == 'regular' or H.validate(hours):
                    raise ValueError(f'Invalid proposed hours: {key}')
            elif hours:
                raise ValueError(f'{action} must not carry proposed hours: {key}')
            self.rows[key] = {**r, 'hours': hours}
        if set(self.rows) != set(targets):
            raise ValueError(f'Engine demo file is missing {len(set(targets)-set(self.rows))} demo targets')
        model_ids = {r['model_id'] for r in records}
        if len(model_ids) != 1:
            raise ValueError('Expected one model export')
        self.model_id = model_ids.pop()
        self.entries = {}
        for key, records in history.items():
            available = [r for r in records if r['entered_at'] < snapshot]
            if available:
                self.entries[key] = max(available, key=lambda r: r['entered_at'])['holidayHours']
        self.dates_by_key = defaultdict(list)
        for r in calendar:
            self.dates_by_key[(r['country'], r['day_key'])].append(date.fromisoformat(r['date']))

    def regular(self, eid, day):
        return H.item_to_str(self.entities[eid]['hours'].get(H.DAYS[day.weekday()])) or 'closed'

    def displayed(self, eid, day):
        if self.entities[eid].get('closed'):
            return 'closed'
        value = H.item_to_str(self.entries.get((eid, day.isoformat())))
        return self.regular(eid, day) if value in (None, 'regular') else value

    def previous_hours(self, eid, day, day_key):
        country = self.entities[eid]['meta']['countryCode']
        previous = sorted((d for d in self.dates_by_key[(country, day_key)]
                           if cycle(d) == cycle(day)-1), reverse=True)
        for d in previous:
            recorded = H.item_to_str(self.entries.get((eid, d.isoformat())))
            if recorded == 'regular':
                versions = [r for r in self.regular_history.get(eid, [])
                            if r['effective_from'] == 'before-2024' or r['effective_from'] <= d.isoformat()]
                if not versions:
                    continue
                version = max(versions, key=lambda r: '' if r['effective_from'] == 'before-2024' else r['effective_from'])
                recorded = H.item_to_str(version['hours'].get(H.DAYS[d.weekday()])) or 'closed'
            if recorded is not None:
                return recorded, d.isoformat()
        return None, None

    def predict(self, eid, day):
        key = (eid, day.isoformat())
        if key not in self.rows:
            raise ValueError('Business/date is outside the selected prediction target set')
        target, prediction = self.targets[key], self.rows[key]
        previous, previous_date = self.previous_hours(eid, day, target['day_key'])
        matches = prediction['action'] == 'SUGGEST' and previous == prediction['hours']
        return {**prediction, 'shown': prediction['shown_hours'], 'key': target['day_key'],
                # This verifies the message's factual comparison, not the model's causal attribution.
                'last_hours': previous if matches else None,
                'last_date': previous_date if matches else None,
                'basis': 'LAST_YEAR' if matches else 'SELECTED_MODEL',
                'has_entered_exception': key in self.entries}
