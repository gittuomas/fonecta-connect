"""Step 5 (Apply): write approved hours to Yext and work out where they will show and when.

Routing: the team's sandbox business goes to the real Yext sandbox, every dataset business
goes to the package's local mock (mock_api/server.py on :8787). Both speak the same API, so
the code path is identical: read the entity, change one holidayHours date, PUT the full hours
object back (Yext replaces the object, so a partial write would drop the other dates).
"""
from __future__ import annotations

import copy
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from functools import lru_cache

from . import config
from .hours import from_yext, to_yext

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class YextError(RuntimeError):
    pass


def _target(entity_id: str) -> tuple[str, str, str]:
    if entity_id == config.YEXT_SANDBOX_ENTITY_ID:
        if not config.YEXT_SANDBOX_KEY:
            raise YextError("YEXT_API_KEY is not set in .env")
        return config.YEXT_SANDBOX_BASE, config.YEXT_SANDBOX_KEY, "sandbox"
    return config.YEXT_MOCK_BASE, "demo", "mock"


def _req(entity_id: str, method: str, body: dict | None = None) -> dict:
    base, key, _ = _target(entity_id)
    path = urllib.parse.quote(entity_id, safe="")
    url = f"{base}/v2/accounts/me/entities/{path}?v={config.YEXT_API_VERSION}"
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for attempt in range(3):                      # the sandbox occasionally resets idle connections
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"api-key": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise YextError(f"{method} entity -> {e.code}: {e.read().decode()[:300]}") from None
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last = e
            time.sleep(0.4 * (attempt + 1))
    raise YextError(f"Yext not reachable ({_target(entity_id)[2]}): {getattr(last, 'reason', last)}")


def target_name(entity_id: str) -> str:
    return "Yext sandbox" if entity_id == config.YEXT_SANDBOX_ENTITY_ID else "Yext mock API"


def get_entity(entity_id: str) -> dict:
    return _req(entity_id, "GET")["response"]


def shown_hours(entity: dict, d: date) -> str:
    """What the listing shows for that date right now: the holiday entry, else the regular weekday hours."""
    hours = entity.get("hours") or {}
    for h in hours.get("holidayHours", []) or []:
        if h.get("date") == d.isoformat():
            if h.get("isRegularHours"):
                break
            return from_yext(h)
    return from_yext(hours.get(DAYS[d.weekday()]))


# Hours object per entity, kept for the length of a call so each decision costs one PUT instead
# of GET + PUT + GET while the owner waits. Filled when the call starts (prefetch) and after every
# write. The read-back check still runs, in the background, and logs a mismatch as an alert.
_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_TTL_S = 600
_CACHE_LOCK = threading.Lock()


def prefetch(entity_id: str) -> None:
    hours = get_entity(entity_id).get("hours") or {}
    with _CACHE_LOCK:
        _CACHE[entity_id] = (time.time(), hours)


def forget(entity_id: str | None = None) -> None:
    with _CACHE_LOCK:
        _CACHE.clear() if entity_id is None else _CACHE.pop(entity_id, None)


def _verify(entity_id: str, d: date, hours: str, on_result) -> None:
    try:
        after = get_entity(entity_id)
        got = [h for h in (after.get("hours") or {}).get("holidayHours", []) if h.get("date") == d.isoformat()]
        ok = bool(got) and from_yext(got[0]) == hours
        if not ok:
            forget(entity_id)
        on_result(ok, got[0] if got else None)
    except YextError as e:
        on_result(False, str(e))


_WRITE_LOCKS: dict[str, threading.Lock] = {}


def _write_lock(entity_id: str) -> threading.Lock:
    with _CACHE_LOCK:
        return _WRITE_LOCKS.setdefault(entity_id, threading.Lock())


def write_holiday_hours(entity_id: str, d: date, hours: str, on_verified=None) -> dict:
    # One writer per entity at a time: each PUT carries the full hours object, so two concurrent
    # writes built from the same cached copy would drop each other's date.
    with _write_lock(entity_id):
        # Always start from a fresh read. Step 5 (WhatsApp) mirrors its writes to the same sandbox business,
        # and a PUT built from an older copy would silently drop its dates. This runs in the background,
        # off the call, so the extra GET costs the owner nothing.
        hours_obj = get_entity(entity_id).get("hours") or {}
        hol = [h for h in hours_obj.get("holidayHours", []) or [] if h.get("date") != d.isoformat()]
        entry = {"date": d.isoformat(), **to_yext(hours)}
        hol.append(entry)
        hours_obj["holidayHours"] = sorted(hol, key=lambda h: h["date"])
        _req(entity_id, "PUT", {"hours": hours_obj})
        with _CACHE_LOCK:
            _CACHE[entity_id] = (time.time(), copy.deepcopy(hours_obj))
    if on_verified:
        threading.Thread(target=_verify, args=(entity_id, d, hours, on_verified), daemon=True).start()
    return {"target": target_name(entity_id), "entity_id": entity_id, "date": d.isoformat(), "written": entry}


def write_async(entity_id: str, d: date, hours: str, on_done, on_verified=None) -> None:
    """Publish in the background so the caller (a live phone call) is not held for the Yext round trip,
    which is 2 to 4 s on the sandbox. on_done(ok, result_or_error) reports the outcome."""
    def run():
        try:
            on_done(True, write_holiday_hours(entity_id, d, hours, on_verified=on_verified))
        except YextError as e:
            on_done(False, str(e))
        except Exception as e:  # noqa: BLE001  (never lose a decision to an unexpected error)
            on_done(False, f"{type(e).__name__}: {e}")
    threading.Thread(target=run, daemon=True).start()


@lru_cache(maxsize=1)
def _listings() -> dict:
    p = config.PKG_DIR / "data" / "listings.json"
    out: dict[str, list] = {}
    for row in json.loads(p.read_text(encoding="utf-8"))["listings"]:
        out.setdefault(row["entityId"], []).append(row)
    return out


def _eta(sec: int) -> str:
    if sec < 3600:
        return f"about {max(1, sec // 60)} minutes"
    if sec < 86400:
        return f"about {sec // 3600} hour{'s' if sec >= 7200 else ''}"
    return f"about {sec // 86400} day{'s' if sec >= 172800 else ''}"


def where_visible(entity_id: str) -> dict:
    """Live publishers and typical update speed (Fonecta: use listings.json for the confirmation step)."""
    key = config.SANDBOX_STANDIN_FOR if entity_id == config.YEXT_SANDBOX_ENTITY_ID else entity_id
    live = sorted([l for l in _listings().get(key, []) if l["status"] == "LIVE"],
                  key=lambda l: l["typicalUpdateSpeedSeconds"])
    rows = [{"publisher": l["publisherName"], "eta": _eta(l["typicalUpdateSpeedSeconds"]),
             "seconds": l["typicalUpdateSpeedSeconds"]} for l in live]
    fast = [r["publisher"] for r in rows if r["seconds"] <= 3600]
    slow = [r for r in rows if r["seconds"] > 3600]
    spoken = ""
    if fast:
        spoken = f"{', '.join(fast[:3])} within the hour"
    if slow:
        spoken += f"; the slowest, {slow[-1]['publisher']}, in {slow[-1]['eta']}"
    return {"publishers": rows, "summary": spoken.strip("; ")}
