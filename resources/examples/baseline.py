#!/usr/bin/env python3
"""Rule-based baseline: 'copy last year → shopping centre → peers'. Standard library only.

    python examples/baseline.py                 # 2026 season, writes outputs/baseline_predictions_2026.csv
    python examples/baseline.py --season 2025   # back-test on the 2025 season (dev entities)

Then score it:
    python evaluation/evaluate.py --predictions outputs/baseline_predictions_2026.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = lambda *p: os.path.join(ROOT, "data", *p)  # noqa: E731
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
SNAP = {2026: date(2026, 11, 6), 2025: date(2025, 11, 6)}
PERM_WORDS = ("lopettanut", "pysyvästi", "stängt för gott", "permanent", "lagt ned", "stengt for godt", "closed down")


def day_str(obj):
    if not obj or obj.get("isClosed"):
        return "closed"
    return ",".join(f"{i['start']}-{i['end']}" for i in obj.get("openIntervals", [])) or "closed"


def cycle(d):
    return d.year if d.month >= 2 else d.year - 1


def load():
    ents = {e["meta"]["id"]: e for e in json.load(open(D("entities.json"), encoding="utf-8"))["response"]["entities"]}
    hol = defaultdict(list)
    for line in open(D("holiday_hours_history.jsonl"), encoding="utf-8"):
        r = json.loads(line)
        hol[(r["entity_id"], r["date"])].append(r)
    reg = defaultdict(list)
    for line in open(D("regular_hours_history.jsonl"), encoding="utf-8"):
        r = json.loads(line)
        reg[r["entity_id"]].append(r)
    cal = list(csv.DictReader(open(D("calendar_special_days.csv"), encoding="utf-8")))
    bench = {(r["country"], r["city_tier"], r["segment"], r["day_key"]): r
             for r in csv.DictReader(open(D("peer_benchmarks.csv"), encoding="utf-8"))}
    tiers = {r["city"]: r["city_tier"] for r in csv.DictReader(open(D("cities.csv"), encoding="utf-8"))}
    reviews = json.load(open(D("reviews.json"), encoding="utf-8"))["response"]["reviews"]
    sugg = json.load(open(D("publisher_suggestions.json"), encoding="utf-8"))["response"]["suggestions"]
    return ents, hol, reg, cal, bench, tiers, reviews, sugg


class Baseline:
    def __init__(self, season):
        self.season, self.snap = season, SNAP[season]
        (self.ents, self.hol, self.reg, self.cal, self.bench, self.tiers, reviews, sugg) = load()
        cutoff = datetime(self.snap.year, self.snap.month, self.snap.day).timestamp() * 1000
        self.closed_signal = set()
        for r in reviews:
            if r["publisherDate"] < cutoff and r["publisherDate"] > cutoff - 150 * 86400000 and \
                    any(w in r["content"].lower() for w in PERM_WORDS):
                self.closed_signal.add(r["locationId"])
        for s in sugg:
            if s["fieldName"] == "closed" and s["dateCreated"] < self.snap.isoformat():
                self.closed_signal.add(s["locationId"])
        self.dates_by_key = defaultdict(list)
        for r in self.cal:
            self.dates_by_key[(r["country"], r["day_key"])].append(date.fromisoformat(r["date"]))

    def regular(self, eid, d: date):
        """Published regular hours as of the snapshot (handles seasonal closures)."""
        cands = [r for r in self.reg[eid] if r["effective_from"] == "before-2024" or
                 r["effective_from"] < self.snap.isoformat()]
        best = max(cands, key=lambda r: "0000" if r["effective_from"] == "before-2024" else r["effective_from"])
        return day_str(best["hours"][DAYS[d.weekday()]])

    def entry(self, eid, d: date, as_of: date):
        rows = [r for r in self.hol.get((eid, d.isoformat()), []) if r["entered_at"] < as_of.isoformat()]
        if not rows:
            return None
        h = max(rows, key=lambda r: r["entered_at"])["holidayHours"]
        return "REGULAR" if h.get("isRegularHours") else day_str(h)

    def displayed(self, eid, d):
        e = self.entry(eid, d, self.snap)
        if e is None or e == "REGULAR":
            return self.regular(eid, d)
        return e

    def explain(self, eid, d: date, key: str):
        """Return (action, hours, basis, evidence). basis: LAST_YEAR | SHOPPING_CENTRE | PEERS | CLOSED_SIGNALS | None"""
        ent = self.ents[eid]
        if eid in self.closed_signal and not ent.get("closed"):
            return "FLAG", "", "CLOSED_SIGNALS", "reviews or Google edits say the business has closed for good"
        shown = self.displayed(eid, d)
        cand, basis, ev = None, None, None
        mall = ent.get("c_shoppingCenterEntityId")
        if mall:
            m = self.entry(mall, d, self.snap)
            if m and m != "REGULAR":
                cand, basis, ev = m, "SHOPPING_CENTRE", self.ents[mall]["name"]
        if cand is None:
            prev = [x for x in self.dates_by_key[(ent["meta"]["countryCode"], key)] if cycle(x) == cycle(d) - 1]
            for p in prev:
                last = self.entry(eid, p, self.snap)
                if last is not None:
                    cand = self.regular(eid, d) if last == "REGULAR" else last
                    basis, ev = "LAST_YEAR", (p.isoformat(), last)
        if cand is None:
            b = self.bench.get((ent["meta"]["countryCode"], self.tiers[ent["address"]["city"]], ent["c_segment"], key))
            if b and b["closed_share_among_entered"] and float(b["closed_share_among_entered"]) >= 0.7 and \
                    float(b["share_entered_closed"]) >= 0.15 and shown != "closed":
                cand, basis, ev = "closed", "PEERS", float(b["closed_share_among_entered"])
        if cand and cand != shown:
            return "SUGGEST", cand, basis, ev
        return "NO_ACTION", "", basis, ev

    def predict(self, eid, d: date, key: str):
        a, h, _, _ = self.explain(eid, d, key)
        return a, h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=2026, choices=[2025, 2026])
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    bl = Baseline(a.season)
    tf = os.path.join(ROOT, "evaluation", "targets_2026.csv" if a.season == 2026 else "targets_2025_backtest.csv")
    out = a.out or os.path.join(ROOT, "outputs", f"baseline_predictions_{a.season}.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    n = 0
    with open(tf, encoding="utf-8") as f, open(out, "w", newline="", encoding="utf-8") as g:
        w = csv.writer(g)
        w.writerow(["entity_id", "date", "action", "hours", "confidence"])
        for r in csv.DictReader(f):
            act, h = bl.predict(r["entity_id"], date.fromisoformat(r["date"]), r["day_key"])
            if act != "NO_ACTION":
                w.writerow([r["entity_id"], r["date"], act, h, 0.6])
                n += 1
    print(f"wrote {n} actions -> {out}")


if __name__ == "__main__":
    main()
