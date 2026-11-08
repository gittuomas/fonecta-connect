#!/usr/bin/env python3
"""Step 5 demo: apply an owner's decision to Yext, verify, confirm where it shows, then undo.

    python -m step5_apply.demo_apply                                   # Kahvila Aurora: closed on Christmas Eve
    python -m step5_apply.demo_apply --hours 10:00-14:00 --decision EDITED
    python -m step5_apply.demo_apply --entity FI-D0012 --actor standing_permission   # apply, then inform
    python -m step5_apply.demo_apply --decision REJECTED               # owner says no: nothing is written
    python -m step5_apply.demo_apply --sandbox                         # also write to the team's Yext sandbox

Runs the challenge's Yext mock in-process. --sandbox needs YEXT_API_KEY and YEXT_SANDBOX_MIRROR in .env.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

from . import confirm as C
from . import config
from . import hours as H
from .apply import Applier, Decision, DecisionItem
from .mock_runtime import start_mock
from .store import AuditLog, StandingPermissions, load_contacts
from .yext import YextClient


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--entity", default="FI-D0001")
    ap.add_argument("--date", default="2026-12-24")
    ap.add_argument("--day-key", default="XMAS_EVE")
    ap.add_argument("--hours", default="closed")
    ap.add_argument("--decision", default="ACCEPTED", choices=["ACCEPTED", "EDITED", "REJECTED", "NO_RESPONSE"])
    ap.add_argument("--actor", default="owner", choices=["owner", "standing_permission"])
    ap.add_argument("--remember", action="store_true", help="owner ticked 'do this every year'")
    ap.add_argument("--lang", default=None, help="override message language (fi/sv/nb/en)")
    ap.add_argument("--no-undo", action="store_true")
    ap.add_argument("--sandbox", action="store_true", help="mirror the write to the Yext sandbox")
    ap.add_argument("--today", default="2026-11-06", help="scenario date (the challenge's 'today')")
    a = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    config.load_dotenv()
    tmp = Path(tempfile.mkdtemp())
    mock = start_mock(str(tmp))
    try:
        run(a, mock.url, tmp)
    finally:
        mock.stop()


def run(a, mock_url: str, tmp: Path):
    primary = YextClient(mock_url, "demo", name="mock")
    sandbox, mirror = None, {}
    if a.sandbox:
        if not config.sandbox_key() or not config.sandbox_mirror():
            sys.exit("--sandbox needs YEXT_API_KEY and YEXT_SANDBOX_MIRROR (e.g. FI-D0001:Cinnamon Swirl Café) in .env")
        sandbox, mirror = YextClient(config.sandbox_url(), config.sandbox_key(), name="sandbox"), config.sandbox_mirror()

    applier = Applier(primary, AuditLog(tmp / "audit.jsonl"), StandingPermissions(tmp / "perms.json"),
                      load_contacts(), sandbox=sandbox, mirror=mirror, today=date.fromisoformat(a.today))

    ent = primary.get_entity(a.entity)
    before = H.holiday_map(ent.get("hours", {})).get(a.date)
    print(f"\n{ent['name']} ({a.entity}) · {a.date} {a.day_key}\n" + "-" * 64)
    print(f"Yext before   : {before or 'no special hours (regular ' + str(H.weekdays(ent['hours'])[_wd(a.date)]) + ')'}")
    print(f"Decision      : {a.actor} · {a.decision} · {a.hours}" + (" · every year" if a.remember else ""))

    d = Decision(a.entity, [DecisionItem(a.date, a.hours, a.decision, a.day_key, seen_before=before)],
                 actor=a.actor, channel="WHATSAPP", suggestion_id="demo-1", remember_yearly=a.remember,
                 language=a.lang)
    res = applier.apply(d)
    for r in res.items:
        print(f"Result        : {r.status}" + (f" – {r.reason}" if r.reason else ""))
    after = H.holiday_map(primary.get_entity(a.entity)["hours"]).get(a.date)
    print(f"Yext after    : {after or 'no special hours'}  (read back from the API)")
    if res.mirror:
        print(f"Sandbox mirror: {json.dumps(res.mirror, ensure_ascii=False)}")
    if res.audit_id:
        print(f"Audit record  : {res.audit_id}")
    pubs = C.live_publishers(a.entity)
    if res.applied and pubs:
        print("Where it shows:")
        for p in pubs:
            print(f"   {p['publisherName']:40s} {C.eta(p['seconds'], 'en')}")
    print("\nMessage to owner:\n" + _indent(res.message or "(none – nothing changed, so we stay quiet)"))

    if res.audit_id and not a.no_undo:
        print("\nOwner replies UNDO …")
        u = applier.undo(res.audit_id, language=a.lang)
        back = H.holiday_map(primary.get_entity(a.entity)["hours"]).get(a.date)
        print(f"Undo result   : {[i.status for i in u.items]} → Yext now: {back or 'no special hours'}")
        print(_indent(u.message or ""))


def _wd(iso):
    return H.DAYS[date.fromisoformat(iso).weekday()]


def _indent(s):
    return "\n".join("   " + line for line in s.splitlines())


if __name__ == "__main__":
    main()
