"""Export source facts in the challenge's event and daily-weather shapes."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .providers import ATTRIBUTIONS, JsonCache, ZONES, fetch_events, utcnow

PACKAGE = Path(__file__).resolve().parent
DATA = PACKAGE.parent / 'resources' / 'data'
DEFAULT_BUNDLE = PACKAGE / '.runtime' / 'context'
WEATHER_FIELDS = ['city', 'country', 'date', 'tmean_c', 'tmin_c', 'tmax_c', 'precip_mm', 'snow_depth_cm', 'is_forecast', 'snowfall_cm']


def read_csv(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def event_record(e):
    return {
        '@id': 'live:' + e['id'], 'name': {'en': e['title']},
        'start_time': e['start'], 'end_time': e['end'],
        'location': {'name': {'en': e['venue']}, 'position': {'coordinates': [e['lon'], e['lat']]}},
        'keywords': [e['category']] if e['category'] else [],
        'x_expected_attendance': None,  # Listing/capacity/ticket availability is not attendance.
        'x_country': e['country'], 'event_status': e['status'], 'info_url': {'en': e['url']},
        'x_provenance': {k: e.get(k) for k in ('provider', 'fetched_at', 'updated_at', 'source_status', 'coordinate_source')},
    }


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def daily_weather(data, city, start, end):
    """Provider daily aggregates in the city's timezone; snow depth is local noon, in cm."""
    daily, units = data.get('daily', {}), data.get('daily_units', {})
    hourly = data.get('hourly', {})
    snow = dict(zip(hourly.get('time', []), hourly.get('snow_depth', [])))
    names = {'tmean_c': 'temperature_2m_mean', 'tmin_c': 'temperature_2m_min',
             'tmax_c': 'temperature_2m_max', 'precip_mm': 'precipitation_sum'}
    rows = []
    for i, day in enumerate(daily.get('time', [])):
        if not start.isoformat() <= day < end.isoformat():
            continue
        row = {'city': city['city'], 'country': city['country'], 'date': day, 'is_forecast': 1}
        for column, name in names.items():
            values = daily.get(name, [])
            value = values[i] if i < len(values) else None
            expected_unit = 'mm' if column == 'precip_mm' else '°C'
            row[column] = value if finite(value) and units.get(name) == expected_unit else None
        depth = snow.get(day + 'T12:00')
        row['snow_depth_cm'] = round(depth * 100, 3) if finite(depth) and depth >= 0 and data.get('hourly_units', {}).get('snow_depth') == 'm' else None
        snowfall = daily.get('snowfall_sum', [])
        snowfall_value = snowfall[i] if i < len(snowfall) else None
        row['snowfall_cm'] = snowfall_value if finite(snowfall_value) and snowfall_value >= 0 and units.get('snowfall_sum') == 'cm' else None
        if any(row[k] is not None for k in names):
            rows.append(row)
    return rows


def fetch_city_weather(cache, city, start, end):
    url = 'https://api.open-meteo.com/v1/forecast?' + urllib.parse.urlencode({
        'latitude': city['lat'], 'longitude': city['lon'], 'forecast_days': 10,
        'timezone': ZONES[city['country']], 'hourly': 'snow_depth',
        'daily': 'temperature_2m_mean,temperature_2m_min,temperature_2m_max,precipitation_sum,snowfall_sum',
    })
    report = {'city': city['city'], 'country': city['country'], 'url': url, 'status': 'unavailable', 'days': 0}
    try:
        data, stamp, state = cache.get(url)
        rows = daily_weather(data, city, start, end)
        report.update(status=state, fetched_at=stamp, days=len(rows),
                      missing_values=sum(r[k] is None for r in rows for k in WEATHER_FIELDS[3:8] + ['snowfall_cm']))
        return rows, report
    except (RuntimeError, ValueError, TypeError, KeyError):
        return [], report


def output_directory(output):
    output = Path(output).resolve()
    protected = (DATA.parent.resolve(), (PACKAGE / 'data').resolve())
    if output in (PACKAGE.resolve(), PACKAGE.parent.resolve()) or any(
            output == p or p in output.parents for p in protected):
        raise ValueError('Write live inputs to a separate output directory; challenge materials and recorded evidence are read-only')
    return output


def write_bundle(output, events, weather, metadata):
    output = output_directory(output)
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / 'local_events.json', {'data': events})
    tmp = output / 'weather_daily.tmp'
    with tmp.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=WEATHER_FIELDS)
        writer.writeheader()
        writer.writerows(weather)
    tmp.replace(output / 'weather_daily.csv')
    metadata = dict(metadata, schema_version=1, research_only=True, event_hours_candidates_enabled=False,
                    files={name: hashlib.sha256((output / name).read_bytes()).hexdigest()
                           for name in ('local_events.json', 'weather_daily.csv')})
    # Publish last; readers reject mixed generations by checksum instead of silently using them.
    write_json(output / 'manifest.json', metadata)
    return metadata


def refresh(output=DEFAULT_BUNDLE, start=None, days=7, max_pages=8, offline=False):
    if not 1 <= days <= 10 or not 1 <= max_pages <= 9:
        raise ValueError('Use 1–10 days and 1–9 pages per event source/country')
    output = output_directory(output)  # Reject protected destinations before making any API calls.
    start = start or utcnow().astimezone(ZoneInfo(ZONES['FI'])).date() + timedelta(days=1)
    end = start + timedelta(days=days)
    cache = JsonCache(PACKAGE / '.runtime' / 'http', offline=offline)
    events, reports = fetch_events(cache, start, days, max_pages)
    cities = read_csv(DATA / 'cities.csv')
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda c: fetch_city_weather(cache, c, start, end), cities))
    weather = [row for rows, _ in results for row in rows]
    return write_bundle(output, [event_record(e) for e in events], weather, {
        'generated_at': utcnow().isoformat(), 'window': {'start': start.isoformat(), 'end_exclusive': end.isoformat()},
        'event_sources': reports, 'weather_sources': [r for _, r in results],
        'event_counts': dict(Counter(e['country'] for e in events)),
        'weather_rows': len(weather), 'weather_cities_requested': len(cities),
        'weather_attribution': ATTRIBUTIONS['weather'],
        'transformations': ['Event facts mapped to the supplied LinkedEvents-like shape; unknown attendance/end times remain null.',
                            'Weather: provider daily local-time aggregates; snow depth at local noon, metres converted to centimetres.',
                            'Missing CSV values are blank, never fabricated as zero. No observed history is supplied.'],
        'limitations': ['Live facts do not establish that published business hours are wrong or that demand will justify an extension.',
                        'Ingestion produces no customer messages, hours or writes. Optional checks ask for owner-confirmed hours.',
                        'The selected frozen ensemble does not use this detailed context variant. Promotion requires validation.',
                        'Current retrievals cannot be treated as available at the 6 November benchmark cutoff.'],
    })
