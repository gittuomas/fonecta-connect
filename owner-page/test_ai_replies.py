#!/usr/bin/env python3
"""Compare the AI model (OpenShift AI) with the simple pattern reader on realistic owner replies.

    python3 owner-page/test_ai_replies.py

Uses Muoti Lyhty's Christmas message (23, 24, 25, 26 Dec; suggested 10-21, 10-13, closed, 11-17).
Prints, for each reply, what each reader understood and whether it matches the expected answer.
"""
import os
import sys
import time
from datetime import date

os.environ.setdefault("PORT", "0")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app        # noqa: E402  (loads the demo data and step 5; does not start the web server)
import hours_ai   # noqa: E402

D = lambda m, d: date(2026, m, d)  # noqa: E731
CASES = [
    ("24 Dec 10-14, 26 Dec closed", {D(12, 24): "10:00-14:00", D(12, 26): "closed"}),
    ("We're closed on Christmas Eve, but on the 23rd we're open until 8", {D(12, 24): "closed", D(12, 23): "10:00-20:00"}),
    ("boxing day 12 to 4 please", {D(12, 26): "12:00-16:00"}),
    ("christmas day closed and xmas eve 9-12", {D(12, 25): "closed", D(12, 24): "09:00-12:00"}),
    ("on the 26th we open at noon and close at 6pm", {D(12, 26): "12:00-18:00"}),
    ("normal hours on the 23rd, rest is fine", {D(12, 23): "regular"}),
    # replies written after tuning, to check it works on wording it hasn't seen
    ("xmas eve we'll be shut, boxing day from 11 till 3", {D(12, 24): "closed", D(12, 26): "11:00-15:00"}),
    ("23.12 open 9 to 9", {D(12, 23): "09:00-21:00"}),
    ("On christmas day we are not open", {D(12, 25): "closed"}),
    ("boxing day: usual hours", {D(12, 26): "regular"}),
    ("the 24th we open at 8 and close at 1", {D(12, 24): "08:00-13:00"}),
]


def main():
    items = app.bundle("FI-D0004", "christmas")[2]
    if not hours_ai.configured():
        sys.exit("OPENSHIFT_AI_URL / OPENSHIFT_AI_TOKEN not set in .env")
    score = {"ai": 0, "pattern": 0}
    for text, want in CASES:
        t = time.time()
        ai = hours_ai.read_reply(text, items)
        took = time.time() - t
        ai_found = ai[0] if ai else None
        pat_found, _ = app.parse_hours_reply(text, items)
        ok_ai, ok_pat = ai_found == want, pat_found == want
        score["ai"] += ok_ai
        score["pattern"] += ok_pat
        fmt = lambda f: "-" if not f else ", ".join(f"{d:%d %b} {h}" for d, h in sorted(f.items()))  # noqa: E731
        print(f"\nOwner: {text}\n  AI model  {'OK ' if ok_ai else 'NO '} {fmt(ai_found) if ai else 'no answer'}  ({took:.1f}s)"
              f"\n  Pattern   {'OK ' if ok_pat else 'NO '} {fmt(pat_found)}")
    print(f"\nCorrect: AI model {score['ai']}/{len(CASES)}, pattern reader {score['pattern']}/{len(CASES)}")


if __name__ == "__main__":
    main()
