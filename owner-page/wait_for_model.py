#!/usr/bin/env python3
"""Wait until the OpenShift AI model answers (checks every 15 s, gives up after 15 min)."""
import json
import os
import time
import urllib.error
import urllib.request

import hours_ai

hours_ai.load_env()
url = os.environ["OPENSHIFT_AI_URL"].rstrip("/") + "/v1/models"
start = time.time()
while time.time() - start < 900:
    try:
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + os.environ["OPENSHIFT_AI_TOKEN"]})
        with urllib.request.urlopen(req, timeout=20, context=hours_ai._ssl_context()) as r:
            print("READY", json.loads(r.read())["data"][0]["id"], f"after {time.time() - start:.0f}s")
            break
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception as e:  # noqa: BLE001
        status = type(e).__name__
    time.sleep(15)
else:
    print("NOT READY after 15 min, last status:", status)
