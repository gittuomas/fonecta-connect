"""When does a text suggestion escalate to a phone call?

A call is the most intrusive channel, so every rule below has to pass. Each check is
returned with its outcome so the decision itself is explainable in the demo and the audit log.

  1. Something to call about: at least one SUGGEST item inside the horizon. FLAG items
     (e.g. "closed for good") are never phoned about; they go to a human at Fonecta.
  2. The owner has not opted out of suggestions.
  3. No standing permission that already covers these dates (then: apply, inform by text).
  4. Confident enough: items under VOICE_MIN_CONFIDENCE are dropped from the call.
  5. A text went out first and got no reply for VOICE_WAIT_HOURS (VOICE_WAIT_HOURS_URGENT when urgent).
  6. Urgent: the first date is at most VOICE_URGENT_DAYS away.
  7. Attempts: at most VOICE_MAX_ATTEMPTS calls per bundle, at least VOICE_MIN_GAP_HOURS apart.
  8. Inside the owner's best contact window, local time, not on a Sunday or on one of the dates.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import config

CHANNEL_NAME = {"SMS": "SMS", "WHATSAPP": "WhatsApp", "EMAIL": "Email", "APP_PUSH": "App push", "VOICE": "Call"}


def demo_now() -> datetime:
    return datetime.fromisoformat(config.DEMO_NOW).replace(tzinfo=ZoneInfo("Europe/Helsinki"))


def _check(name: str, ok: bool, detail: str) -> dict:
    return {"check": name, "ok": bool(ok), "detail": detail}


def evaluate(bundle: dict, outreach: list[dict], calls: list[dict], now: datetime, mode: str) -> dict:
    tz = ZoneInfo(bundle.get("timezone", "Europe/Helsinki"))
    local = now.astimezone(tz)
    items = [it for it in bundle["items"] if it["confidence"] >= config.VOICE_MIN_CONFIDENCE]
    dropped = len(bundle["items"]) - len(items)
    checks = []

    checks.append(_check("something_to_call_about", bool(items),
                         f"{len(items)} suggestion(s) within {config.VOICE_HORIZON_DAYS} days"
                         + (f"; {len(bundle['flags'])} flag(s) routed to a human instead" if bundle["flags"] else "")))
    checks.append(_check("not_opted_out", not bundle["opted_out"],
                         "owner opted out of suggestions" if bundle["opted_out"] else "owner accepts suggestions"))
    checks.append(_check("no_standing_permission", not bundle["standing_permission_holidays"],
                         "standing permission: apply and inform by text, no call needed"
                         if bundle["standing_permission_holidays"] else "no standing permission for holidays"))
    checks.append(_check("confident", bool(items),
                         f"all items ≥ {config.VOICE_MIN_CONFIDENCE:.2f} confidence"
                         if not dropped else f"{dropped} low-confidence item(s) left out of the call"))

    first = min((date.fromisoformat(it["date"]) for it in items), default=None)
    days_left = (first - local.date()).days if first else None
    urgent = days_left is not None and days_left <= config.VOICE_URGENT_DAYS
    checks.append(_check("urgent", urgent,
                         f"first date in {days_left} day(s) (limit {config.VOICE_URGENT_DAYS})"
                         if days_left is not None else "no dates"))

    texts = [o for o in outreach if o.get("channel") != "VOICE"]
    wait_h = config.VOICE_WAIT_HOURS_URGENT if urgent else config.VOICE_WAIT_HOURS
    if texts:
        last = max(texts, key=lambda o: o["sent_at"])
        sent = datetime.fromisoformat(last["sent_at"])
        hours = (now - sent).total_seconds() / 3600
        replied = any(o.get("replied") for o in texts)
        ok = (not replied) and hours >= wait_h
        ch = CHANNEL_NAME.get(last["channel"], last["channel"])
        detail = (f"owner already replied by {ch}" if replied else
                  f"{ch} message sent {hours:.0f} h ago, no reply (wait {wait_h:.0f} h)")
    else:
        ok, detail = False, "no text sent yet: message first, call only if it goes unanswered"
    checks.append(_check("text_unanswered", ok, detail))

    attempts = [c for c in calls if c.get("status") not in ("created_not_placed",)]
    gap_ok = all((now - datetime.fromisoformat(c["created_at"])).total_seconds() / 3600 >= config.VOICE_MIN_GAP_HOURS
                 for c in attempts)
    checks.append(_check("attempts_left", len(attempts) < config.VOICE_MAX_ATTEMPTS and gap_ok,
                         f"{len(attempts)} of {config.VOICE_MAX_ATTEMPTS} call attempts used"
                         + ("" if gap_ok else f", last one under {config.VOICE_MIN_GAP_HOURS:.0f} h ago")))

    a, b = (int(x) for x in bundle["best_contact_window"].split("-"))
    in_window = a <= local.hour < b and local.weekday() != 6 and local.date().isoformat() not in \
        {it["date"] for it in items}
    nxt = None
    if not in_window:
        cand = local.replace(hour=a, minute=0, second=0, microsecond=0)
        if cand <= local:
            cand += timedelta(days=1)
        while cand.weekday() == 6:
            cand += timedelta(days=1)
        nxt = cand.isoformat(timespec="minutes")
    checks.append(_check("contact_window", in_window,
                         f"owner prefers {a:02d}–{b:02d}, it is {local:%a %H:%M} local time"
                         + (f"; next window {nxt}" if nxt else "")))

    if mode == "phone":
        checks.append(_check("phone_number", bool(bundle.get("mobile")), "mobile number on file"
                             if bundle.get("mobile") else "no mobile number"))

    hard_fail = [c for c in checks if not c["ok"] and c["check"] != "contact_window"]
    decision = "CALL" if not hard_fail and in_window else ("SCHEDULE" if not hard_fail else "NO_CALL")
    return {"decision": decision, "checks": checks, "items": [it["item"] for it in items],
            "scheduled_for": nxt if decision == "SCHEDULE" else None, "evaluated_at": local.isoformat(timespec="minutes")}
