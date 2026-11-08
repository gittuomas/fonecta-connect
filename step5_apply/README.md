# Apply, verify and undo

Step 5 receives an owner decision, reads the latest entity, checks for conflicts, writes the full hours object, verifies the result and records an audit. Undo restores the recorded prior state without overwriting intervening changes. Publisher names and timing estimates come from the supplied listing data.

## Run

From the repository root with Python 3.12+:

```sh
python -m step5_apply.service
python -m step5_apply.demo_apply --lang en
python -m unittest discover -s step5_apply/tests -t .
```

The HTTP service listens on `127.0.0.1:8790`. The command-line demo applies a decision and undoes it. The [API contract](INTEGRATION.md) describes decisions, snapshots, status responses and standing permissions.

```python
from step5_apply.api import Step5
s5 = Step5.from_env()
seen = s5.snapshot("FI-D0001", ["2026-12-24"])
result = s5.apply({"entity_id": "FI-D0001", "date": "2026-12-24",
                   "hours": "closed", "seen_before": seen["2026-12-24"]})
s5.close()
```

## Destinations

`CONNECT_APPLY_MODE=auto` prefers a mapped real sandbox after a read-only startup check and otherwise selects the local simulation. `sandbox` requires valid real configuration; `demo` explicitly selects the challenge simulator with optional sandbox mirroring. `CONNECT_RUNTIME_DIR` selects a separate local runtime; `CONNECT_ENV_FILE` selects a separate credentials file.

Real WhatsApp uses `Step5.from_env(mode="sandbox")`: the mapped sandbox entity is the primary destination, mappings must be one-to-one, and configuration failures never substitute a simulated write. See [real messaging setup](../app/MESSAGING.md).

Audit logs, permissions and local simulator state are ignored by Git. Keep them across restarts when undo or recovery is needed. Deleting local state cannot undo a real write.
