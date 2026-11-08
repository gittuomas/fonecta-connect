"""Prepare live events + weather, preflight Yext, and launch Laura's owner app.

This command never applies hours. The existing owner decision handlers own writes.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from step5_apply import config
from step5_apply.yext import YextClient, YextError
from .checks import build_checks
from .context import LiveContext
from .datasets import DEFAULT_BUNDLE, output_directory, refresh, write_json

ROOT = Path(__file__).resolve().parent.parent


def preflight(checks, sandbox):
    """Read-only verification. Never silently substitute the mock for requested Yext."""
    if not sandbox:
        return {'mode': 'local_mock', 'mapped_checks': []}
    if checks.get('mode') != 'live':
        raise ValueError('Yext sandbox requires fresh live checks, not recorded replay')
    if not config.sandbox_key():
        raise ValueError('Add YEXT_API_KEY to the ignored .env before starting --sandbox')
    parsed = urlparse(config.sandbox_url())
    if parsed.scheme != 'https' or parsed.hostname != 'sbx-api.yextapis.com':
        raise ValueError('This demo requires the Yext sandbox URL https://sbx-api.yextapis.com')
    mapping = config.sandbox_mirror()
    pairs = [{'entity_id': r['entity_id'], 'check_id': r['id'], 'date': r['date'],
              'sandbox_entity': mapping[r['entity_id']]} for r in checks['checks'] if r['entity_id'] in mapping]
    if not pairs:
        ids = ', '.join(r['entity_id'] for r in checks['checks'])
        raise ValueError(f'No selected check has a YEXT_SANDBOX_MIRROR mapping. Selected businesses: {ids}. '
                         'Add one business:your sandbox entity ID to the mapping; do not invent event matches.')
    client = YextClient(config.sandbox_url(), config.sandbox_key(), name='sandbox', timeout=12, max_retries=0)
    for target in sorted({r['sandbox_entity'] for r in pairs}):
        try:
            entity = client.get_entity(target)
            if not isinstance(entity.get('hours'), dict):
                raise ValueError('Mapped Yext entity has no hours object')
        except YextError as exc:
            raise ValueError(f'Yext sandbox preflight failed (HTTP {exc.status}); verify key, entity ID and access') from None
    return {'mode': 'yext_sandbox', 'read_verified': True, 'mapped_checks': pairs}


def prepare(*, sandbox=False, refresh_sources=True, bundle=DEFAULT_BUNDLE, limit=3, analytics=True, output=None):
    config.load_dotenv()
    if sandbox and not config.sandbox_key():
        raise ValueError('Add YEXT_API_KEY to the ignored .env before starting --sandbox')
    output = Path(output or DEFAULT_BUNDLE.parent/'checks.json').resolve()
    output_directory(output.parent)
    if refresh_sources:
        refresh(output=bundle)  # Both live datasets, independently covering all weather cities.
    checks = build_checks(bundle, limit=limit, with_analytics=analytics)
    if not checks['checks'] and sandbox:
        raise ValueError('No fresh eligible checks. Refresh sources or use the ordinary C13 owner demo; do not force an event.')
    target = preflight(checks, sandbox)
    context = LiveContext(bundle)
    weather = [r for (city, day), r in context.forecasts.items() if context.weather_for(city, day)]
    write_json(output, checks)
    report = {'checks_file': str(output), 'check_count': len(checks['checks']), 'yext': target,
              'sources': {'events': context.manifest['event_counts'], 'weather_provider': 'Open-Meteo',
                          'fresh_weather_city_days': len(weather), 'fresh_weather_cities': len({r['city'] for r in weather}),
                          'requested_weather_cities': context.manifest['weather_cities_requested'],
                          'manifest': str(Path(bundle).resolve()/'manifest.json')},
              'note': 'Preparation performs no listing writes. Owner decisions use owner-page -> Step5 -> Yext.'}
    write_json(output.with_name('demo-readiness.json'), report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sandbox', action='store_true', help='Require and read-verify the configured real Yext sandbox')
    parser.add_argument('--env-file', type=Path, help='Read credentials from an existing ignored .env in another checkout')
    parser.add_argument('--use-existing', action='store_true', help='Use the current bundle; freshness checks still apply')
    parser.add_argument('--prepare-only', action='store_true', help='Refresh/select/preflight without starting the web server')
    parser.add_argument('--no-analytics', action='store_true')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--runtime-dir', type=Path, help='Isolated Apply audit, mock state and owner learning for a rehearsal')
    args = parser.parse_args()
    if args.env_file:
        os.environ['CONNECT_ENV_FILE'] = str(args.env_file.resolve())
    try:
        report = prepare(sandbox=args.sandbox, refresh_sources=not args.use_existing, analytics=not args.no_analytics)
    except ValueError as exc:
        parser.exit(2, f'{exc}\n')
    print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)
    if args.prepare_only:
        return 0
    env = dict(os.environ, CONNECT_LIVE_CHECKS=report['checks_file'], PORT=str(args.port))
    if args.runtime_dir:
        env['CONNECT_RUNTIME_DIR'] = str(args.runtime_dir.resolve())
    env['CONNECT_APPLY_MODE'] = 'sandbox' if args.sandbox else 'demo'
    if not args.sandbox:
        env['YEXT_SANDBOX_MIRROR'] = 'disabled'
    return subprocess.call([sys.executable, str(ROOT/'owner-page/app.py')], cwd=ROOT, env=env)


if __name__ == '__main__':
    raise SystemExit(main())
