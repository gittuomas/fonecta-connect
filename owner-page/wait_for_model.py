#!/usr/bin/env python3
"""Wait until the OpenShift AI model answers (checks every 15 s, gives up after 15 min)."""
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hours_ai

from datetime import date
from step5_apply.config import load_dotenv
load_dotenv()
if not all(os.environ.get(k) for k in ('OPENSHIFT_AI_URL', 'OPENSHIFT_AI_TOKEN')):
    raise SystemExit('Set OPENSHIFT_AI_URL and OPENSHIFT_AI_TOKEN in .env first.')
start = time.time()
while time.time() - start < 900:
    found, issues = hours_ai.read_reply('Christmas Eve noon to 6pm',
        [{'day': date(2026, 12, 24), 'day_name': 'Christmas Eve'}])
    if not issues and found == {date(2026, 12, 24): '12:00-18:00'}:
        print('READY: inference verified', f'after {time.time() - start:.0f}s')
        break
    status = '; '.join(issues) or 'Unexpected parsed hours'
    time.sleep(15)
else:
    print("NOT READY after 15 min, last status:", status)
    raise SystemExit(2)
