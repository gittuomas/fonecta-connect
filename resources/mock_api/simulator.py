"""Owner (business customer) response simulator. Shared by the generator (2025 pilot log) and the mock API."""
from __future__ import annotations

import hashlib
import math
import random

from hours import boundary_diff, same_state  # noqa: E402

CHANNEL_FACTOR = {"SMS": 1.0, "WHATSAPP": 1.1, "EMAIL": 0.75, "APP_PUSH": 0.6}
LATENCY_MEDIAN_MIN = {"SMS": 90, "WHATSAPP": 45, "EMAIL": 900, "APP_PUSH": 600}


def _rng(*parts) -> random.Random:
    h = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return random.Random(int(h[:16], 16))


def respond(truth: str, proposed: str, responsiveness: float, channel: str, *seed_parts, send_hour: int = 10):
    """Return dict(decision, final_hours, latency_minutes, standing_permission).

    decision in ACCEPTED | EDITED | REJECTED | NO_RESPONSE
    """
    r = _rng(*seed_parts, truth, proposed, channel)
    hour_factor = 1.0 if 8 <= send_hour <= 17 else (0.8 if 18 <= send_hour <= 21 else 0.55)
    p = min(0.97, responsiveness * CHANNEL_FACTOR.get(channel, 0.7) * hour_factor)
    if r.random() > p:
        return {"decision": "NO_RESPONSE", "final_hours": None, "latency_minutes": None,
                "standing_permission": False}
    med = LATENCY_MEDIAN_MIN.get(channel, 600)
    latency = int(max(1, math.exp(math.log(med) + r.gauss(0, 1.0))))
    if proposed == truth:
        dec, final = "ACCEPTED", truth
    elif same_state(proposed, truth) and boundary_diff(proposed, truth) <= 60:
        dec, final = ("EDITED", truth) if r.random() < 0.85 else ("ACCEPTED", proposed)
    elif same_state(proposed, truth):
        dec, final = ("EDITED", truth) if r.random() < 0.7 else ("REJECTED", None)
    else:
        dec, final = ("REJECTED", None) if r.random() < 0.6 else ("EDITED", truth)
    perm = dec == "ACCEPTED" and r.random() < 0.12
    return {"decision": dec, "final_hours": final, "latency_minutes": latency, "standing_permission": perm}
