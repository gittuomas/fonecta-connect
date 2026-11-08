"""Explicit, reversible rehearsal through the real owner HTTP backend.

Run with --run to approve and edit FI-D0001 in the Yext sandbox, read back each
change, and undo it. Without --run, no app is started and nothing is written.
No WhatsApp is sent. Original hours and the audit stay in an ignored runtime.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path


def canonical(hours):
    result = copy.deepcopy(hours)
    result['holidayHours'] = sorted(result.get('holidayHours', []), key=lambda x: x['date'])
    return result


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Authorize temporary real sandbox writes and undo')
    args = parser.parse_args()
    if not args.run:
        parser.print_help()
        return
    root = Path(__file__).resolve().parent
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    runtime = root / '.runtime' / ('sandbox-rehearsal-' + stamp)
    runtime.mkdir(parents=True)
    os.environ['CONNECT_APPLY_MODE'] = 'sandbox'
    os.environ['CONNECT_RUNTIME_DIR'] = str(runtime)
    import app
    from step5_apply import hours as H

    eid, pid = 'FI-D0001', 'christmas'
    s5 = app.S5
    if s5.applier.primary.name != 'sandbox' or eid not in s5.applier.primary.mapping:
        raise RuntimeError('Requires the mapped FI-D0001 real sandbox; no simulation fallback')
    original = canonical(s5.applier.primary.get_entity(eid)['hours'])
    (runtime / 'original-hours.json').write_text(json.dumps(original, indent=2), encoding='utf-8')
    proposal = app.bundle(eid, pid)
    if not proposal:
        raise RuntimeError('No pending Christmas proposal. Rehearsal will not invent one or erase saved hours.')
    server = ThreadingHTTPServer(('127.0.0.1', 0), app.App)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    path = f'/w/{eid}/{pid}'
    checks = []
    report = {'entity_id': eid, 'target': 'real Yext sandbox', 'checks': checks,
              'ai_configured': app.hours_ai.configured(), 'restored': False}

    def request(suffix='', form=None):
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        with urllib.request.urlopen(base + path + suffix, data=data, timeout=90) as response:
            return response.read().decode('utf-8')

    def current():
        return canonical(s5.applier.primary.get_entity(eid)['hours'])

    def require(condition, name):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    try:
        request()
        request(form={'a': 'no'})
        require(current() == original, 'Decline leaves all real sandbox hours unchanged')
        request('/reset')
        request(form={'a': 'yes'})
        expected = canonical(H.with_holiday_changes(original, {
            s['day'].isoformat(): s['hours'] for s in proposal[2]}))
        require(current() == expected and expected != original,
                'Approve writes the proposed hours; real read-back matches and other dates are preserved')
        request(form={'a': 'yes'})
        require(len([r for r in s5.audit(eid) if r['type'] == 'APPLY']) == 1,
                'Repeated approval does not create another apply')
        request('/reply', {'msg': 'UNDO'})
        require(current() == original, 'Approval undo restores the entire original hours object')
        request('/reset')
        request(form={'a': 'other'})
        day = proposal[2][0]['day']
        before = H.holiday_map(original).get(day.isoformat())
        edit = '11:15-14:45' if before != '11:15-14:45' else '10:15-15:45'
        preview = request('/reply', {'msg': f'{day.day} {day:%b} {edit}'})
        require('Save these hours' in preview and current() == original,
                'Editing creates a review draft without a real write')
        request('/reply', {'msg': 'CANCEL'})
        require(current() == original, 'Cancel discards the draft without changing real hours')
        request('/reply', {'msg': f'{day.day} {day:%b} {edit}'})
        request('/reply', {'msg': 'CONFIRM'})
        expected = canonical(H.with_holiday_changes(original, {day.isoformat(): edit}))
        require(current() == expected, 'Confirm writes only the edited date and verifies it in Yext')
        request('/reply', {'msg': 'UNDO'})
        require(current() == original, 'Edit undo restores all original hours')
        report['passed'] = True
    finally:
        # Undo this isolated rehearsal's remaining recorded changes on failure.
        # Step 5 conflict protection prevents overwriting intervening owner edits.
        for record in reversed(s5.audit(eid)):
            if record['type'] == 'APPLY' and not s5.applier.audit.undone(record['id']):
                s5.undo(audit_id=record['id'], language='en')
        report['restored'] = current() == original
        (runtime / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        server.shutdown()
        server.server_close()
        thread.join()
        s5.close()
        print('Original hours restored:', report['restored'], flush=True)
        print('Report:', runtime / 'report.json', flush=True)
        if not report['restored']:
            raise RuntimeError('Original hours differ. Inspect the saved snapshot and audit; do not overwrite blindly.')


if __name__ == '__main__':
    run()
