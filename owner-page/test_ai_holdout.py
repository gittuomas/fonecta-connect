#!/usr/bin/env python3
"""Held-out test of the OpenShift AI reply reader: 40 owner replies it was never tuned on.

    python3 owner-page/test_ai_holdout.py

Written on 7 Nov 2026 AFTER the reader was finished. The expected answers were written down before the
first run, and nothing in hours_ai.py was changed because of these results. (test_ai_replies.py has the
11 replies that were used while building the reader.)

Four demo businesses and both holiday periods, so the reader sees different dates and suggested hours.
When the owner gives only one end of the day ("we close at 4"), the other end comes from the suggested
hours in the message: that is the reader's designed behaviour. Two replies are deliberately vague; the
right answer there is to save nothing and ask the owner.
"""
import json
import os
import sys
import time
from datetime import date

os.environ.setdefault("PORT", "0")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app        # noqa: E402  (loads the demo data and step 5; does not start the web server)
import hours_ai   # noqa: E402

D = lambda y, m, d: date(y, m, d)  # noqa: E731
X = lambda d: D(2026, 12, d)       # noqa: E731  December 2026
J = lambda d: D(2027, 1, d)        # noqa: E731  January 2027

# (business, period, reply, expected). Suggested hours in each message:
#   FI-D0004 christmas: 23 Dec 10-21, 24 Dec 10-13, 25 Dec closed, 26 Dec 11-17
#   FI-D0004 new-year:  31 Dec 10-18, 1 Jan closed, 5 Jan 10-17, 6 Jan 12-18
#   FI-D0003 christmas: 23 Dec 09-20, 24 Dec 09-13, 25 Dec 10-14
#   FI-D0002 new-year:  31 Dec closed, 1 Jan closed, 5 Jan 16-21, 6 Jan closed
CASES = [
    ("FI-D0004", "christmas", "Christmas Eve we close already at 12", {X(24): "10:00-12:00"}),
    ("FI-D0004", "christmas", "on the 23rd we stay open till 10pm", {X(23): "10:00-22:00"}),
    ("FI-D0004", "christmas", "boxing day closed this year", {X(26): "closed"}),
    ("FI-D0004", "christmas", "24th: 9-14", {X(24): "09:00-14:00"}),
    ("FI-D0004", "christmas", "Xmas eve 8am to 2pm and boxing day noon to 5", {X(24): "08:00-14:00", X(26): "12:00-17:00"}),
    ("FI-D0004", "christmas", "we're open on christmas day this time, 12 to 4", {X(25): "12:00-16:00"}),
    ("FI-D0004", "christmas", "Boxing day we open at 1pm", {X(26): "13:00-17:00"}),
    ("FI-D0004", "christmas", "The 23rd normal opening hours please", {X(23): "regular"}),
    ("FI-D0004", "christmas", "Christmas Eve: half past nine until one", {X(24): "09:30-13:00"}),
    ("FI-D0004", "christmas", "closed 24th and 26th", {X(24): "closed", X(26): "closed"}),
    ("FI-D0004", "christmas", "We might close a bit earlier on Christmas Eve, not sure yet", {}),
    ("FI-D0004", "christmas", "23 Dec 10-19", {X(23): "10:00-19:00"}),
    ("FI-D0004", "christmas", "christmas eve shut all day", {X(24): "closed"}),
    ("FI-D0004", "christmas", "On the 26th from 10 until 6", {X(26): "10:00-18:00"}),

    ("FI-D0004", "new-year", "New Year's Eve we close at 4", {X(31): "10:00-16:00"}),
    ("FI-D0004", "new-year", "nye 10-15", {X(31): "10:00-15:00"}),
    ("FI-D0004", "new-year", "Epiphany closed", {J(6): "closed"}),
    ("FI-D0004", "new-year", "new years day we're open 12-16", {J(1): "12:00-16:00"}),
    ("FI-D0004", "new-year", "Epiphany eve usual hours, epiphany 11 to 3", {J(5): "regular", J(6): "11:00-15:00"}),
    ("FI-D0004", "new-year", "31.12 open until 2pm", {X(31): "10:00-14:00"}),
    ("FI-D0004", "new-year", "the 6th we open at 11", {J(6): "11:00-18:00"}),
    ("FI-D0004", "new-year", "We are closed on New Year's Eve and New Year's Day", {X(31): "closed", J(1): "closed"}),
    ("FI-D0004", "new-year", "5 Jan 9-17", {J(5): "09:00-17:00"}),
    ("FI-D0004", "new-year", "on new year's eve from 9 to 13", {X(31): "09:00-13:00"}),

    ("FI-D0003", "christmas", "Christmas Day we're actually closed", {X(25): "closed"}),
    ("FI-D0003", "christmas", "christmas eve 8 to 12", {X(24): "08:00-12:00"}),
    ("FI-D0003", "christmas", "23rd: open till 9pm", {X(23): "09:00-21:00"}),
    ("FI-D0003", "christmas", "xmas day 11-15", {X(25): "11:00-15:00"}),
    ("FI-D0003", "christmas", "on the 24th we open at 10 and close at noon", {X(24): "10:00-12:00"}),
    ("FI-D0003", "christmas", "regular hours on christmas eve", {X(24): "regular"}),
    ("FI-D0003", "christmas", "Christmas Eve closed, Christmas Day closed", {X(24): "closed", X(25): "closed"}),

    ("FI-D0002", "new-year", "New Year's Eve we are open 17 to 23", {X(31): "17:00-23:00"}),
    ("FI-D0002", "new-year", "epiphany eve 18-22", {J(5): "18:00-22:00"}),
    ("FI-D0002", "new-year", "On the 6th of January we open from 4pm to 10pm", {J(6): "16:00-22:00"}),
    ("FI-D0002", "new-year", "Epiphany eve closed", {J(5): "closed"}),
    ("FI-D0002", "new-year", "new year's day 16-21", {J(1): "16:00-21:00"}),
    ("FI-D0002", "new-year", "we'll be open on Epiphany, 5pm till 11pm", {J(6): "17:00-23:00"}),
    ("FI-D0002", "new-year", "Not sure about New Year's Eve yet, I'll let you know", {}),
    ("FI-D0002", "new-year", "Epiphany eve: from six to ten in the evening", {J(5): "18:00-22:00"}),
    ("FI-D0002", "new-year", "1.1. closed", {J(1): "closed"}),
]


def fmt(found):
    return "nothing saved" if not found else ", ".join(f"{d:%d %b} {h}" for d, h in sorted(found.items()))


def main():
    print("Historical exploratory cases: inferred missing hours and regular-hours commands now require clarification. Use --check-services for current readiness.")
    if not hours_ai.configured():
        sys.exit("OPENSHIFT_AI_URL / OPENSHIFT_AI_TOKEN not set in .env")
    rows, score = [], {"ai": 0, "pattern": 0}
    for eid, pid, text, want in CASES:
        items = app.bundle(eid, pid)[2]
        t = time.time()
        ai = hours_ai.read_reply(text, items)
        took = time.time() - t
        used_model = True
        ai_found = ai[0] if ai else None
        pat_found, _ = app.parse_hours_pattern(text, items)
        ok_ai, ok_pat = ai_found == want, pat_found == want
        score["ai"] += ok_ai
        score["pattern"] += ok_pat
        rows.append({"business": eid, "period": pid, "reply": text, "expected": fmt(want),
                     "ai": fmt(ai_found) if ai else "no answer", "ai_ok": ok_ai, "model_used": used_model,
                     "pattern": fmt(pat_found), "pattern_ok": ok_pat, "seconds": round(took, 1)})
        print(f"{'OK ' if ok_ai else 'NO '} {'model' if used_model else 'rules'} {took:5.1f}s  {text}\n"
              f"      got: {rows[-1]['ai']}   expected: {fmt(want)}   pattern reader: {'OK' if ok_pat else 'no'}",
              flush=True)
    n = len(CASES)
    print(f"\nCorrect: AI reader {score['ai']}/{n}, pattern reader {score['pattern']}/{n}")
    out = os.environ.get("HOLDOUT_JSON")
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"score": score, "n": n, "rows": rows}, f, indent=1)


if __name__ == "__main__":
    main()
