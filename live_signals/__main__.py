"""Data ingestion and feature inspection; the owner experience remains in app."""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime

from step5_apply.config import load_dotenv
from pathlib import Path
from .datasets import DATA, DEFAULT_BUNDLE, output_directory, refresh, write_json


def inspect_context(directory, entity_id, day, as_of=None, features=False):
    from .context import LiveContext

    context = LiveContext(directory, as_of=as_of)
    entities = {e['meta']['id']: e for e in json.loads((DATA / 'entities.json').read_text(encoding='utf-8'))['response']['entities']}
    if entity_id not in entities:
        raise ValueError('Unknown challenge business')
    ent = entities[entity_id]
    target = {'entity_id': entity_id, 'date': day.isoformat(), 'day_key': 'CONTEXT', 'day_type': 'CONTEXT', 'event_id': ''}
    evidence = context.evidence(target, ent)
    if evidence['event']:
        target.update(day_key='EVENT', day_type='EVENT', event_id=evidence['event']['@id'])
    result = {'business': ent['name'], 'entity_id': entity_id, 'date': day.isoformat(),
              'integration': 'staged local_events.json and weather_daily.csv replacements; frozen C13 unchanged',
              'evidence': evidence, 'additional_candidate_hours': {},
              'note': 'Evidence inspection only. No prediction, customer message or listing update. Business inputs remain the synthetic 6 Nov snapshot.'}
    if features:
        try:
            from prediction_lab.context_features import ContextFeatureStore
        except ModuleNotFoundError as exc:
            raise ValueError('--features needs the prediction-lab research checkout and its dependencies; omit it to inspect the live evidence on main') from exc
        store = ContextFeatureStore(context=context)
        sources, values = store.extra_context(target)
        result.update(integration='prediction_lab.context_features.ContextFeatureStore',
                      context_features=values, additional_candidate_hours=sources)
    return result


def main():
    parser = argparse.ArgumentParser(description='Live Nordic evidence and owner hours checks')
    commands = parser.add_subparsers(dest='command', required=True)
    update = commands.add_parser('refresh', help='Write local_events.json, weather_daily.csv and a provenance manifest')
    update.add_argument('--output', default=str(DEFAULT_BUNDLE))
    update.add_argument('--start', type=date.fromisoformat)
    update.add_argument('--days', type=int, default=7)
    update.add_argument('--max-pages', type=int, default=8)
    update.add_argument('--offline', action='store_true')
    inspect = commands.add_parser('inspect', help='Inspect source evidence; never proposes hours')
    inspect.add_argument('entity_id')
    inspect.add_argument('date', type=date.fromisoformat)
    inspect.add_argument('--bundle', default=str(DEFAULT_BUNDLE))
    inspect.add_argument('--as-of', type=datetime.fromisoformat, help='Timezone-aware evidence cutoff, for reproducible inspection')
    inspect.add_argument('--features', action='store_true', help='Also inspect the optional prediction-lab research features')
    checks = commands.add_parser('checks', help='Select relevant live events for owner hours checks; never invent hours')
    checks.add_argument('--bundle', default=str(DEFAULT_BUNDLE))
    checks.add_argument('--as-of', type=datetime.fromisoformat, help='Explicit recorded replay cutoff; local-mock demo only')
    checks.add_argument('--limit', type=int, default=3)
    checks.add_argument('--output', default=str(DEFAULT_BUNDLE.parent/'checks.json'))
    checks.add_argument('--no-analytics', action='store_true')
    args = parser.parse_args()
    load_dotenv()
    if args.command == 'refresh':
        report = refresh(args.output, args.start, args.days, args.max_pages, args.offline)
        result = {k: report[k] for k in ('generated_at', 'event_counts', 'weather_rows', 'weather_cities_requested', 'limitations')}
        result['output'] = str(args.output)
    elif args.command == 'checks':
        from .checks import build_checks
        output_directory(Path(args.output).parent)
        report = build_checks(args.bundle, args.as_of, args.limit, not args.no_analytics)
        write_json(args.output, report)
        result = {k: report[k] for k in ('mode', 'report', 'analytics', 'scope')}
        result['output'] = str(args.output)
        result['checks'] = [{k: r[k] for k in ('id', 'entity_id', 'date', 'country', 'published_hours', 'proposed_hours', 'observed_weekday_search_views')} for r in report['checks']]
    else:
        result = inspect_context(args.bundle, args.entity_id, args.date, args.as_of, args.features)
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
