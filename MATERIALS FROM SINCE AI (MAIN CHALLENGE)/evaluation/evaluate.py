#!/usr/bin/env python3
"""Score suggestion predictions against labels.

    python evaluation/evaluate.py --predictions my_predictions.csv                       # dev, 2026 season
    python evaluation/evaluate.py --predictions bt.csv --labels labels/dev_labels_2025_backtest.csv

Prediction CSV columns:
    entity_id, date, action, hours[, confidence]
    action: SUGGEST | NO_ACTION | FLAG      (rows you leave out count as NO_ACTION)
    hours:  'closed' | '10:00-15:00' | '10:00-14:00,17:00-22:00'   (required for SUGGEST)

Points per target row (score = 100 * points / rows that need a suggestion or a flag):
    needs a change:  exact +1 | same open/closed state, both ends within 30 min +0.6 | same state +0.2
                     | wrong state (open<->closed) -1 | FLAG +0.05 | NO_ACTION 0
    already correct: NO_ACTION 0 | FLAG -0.1 | SUGGEST same as shown -0.1 | SUGGEST other -0.5
                     | SUGGEST wrong state -1
    business closed for good (listing still open): FLAG +1 | SUGGEST 'closed' +0.3 | SUGGEST open -1

Also reported: search_views_protected_pct – share of expected search views on wrong-hours days that your exact or
near suggestions fix (findability), and closed_door_views_prevented_pct – the same for 'shown open, actually closed'.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOURS_RE = re.compile(r"^(closed|\d{2}:\d{2}-\d{2}:\d{2}(,\d{2}:\d{2}-\d{2}:\d{2})*)$")


def ivs(s):
    if s == "closed":
        return []
    out = []
    for iv in s.split(","):
        a, b = iv.split("-")
        sa, sb = int(a[:2]) * 60 + int(a[3:]), int(b[:2]) * 60 + int(b[3:])
        if sb <= sa and not (a == "00:00" and b == "23:59"):
            sb += 1440
        out.append((sa, sb))
    return out


def norm(s):
    s = (s or "").strip().lower().replace(" ", "")
    if s in ("", "closed", "kiinni", "stängt", "stengt"):
        return "closed"
    return s


def near(a, b, tol=30):
    ia, ib = ivs(a), ivs(b)
    if not ia or not ib:
        return not ia and not ib
    return abs(ia[0][0] - ib[0][0]) <= tol and abs(ia[-1][1] - ib[-1][1]) <= tol


def load_predictions(path):
    preds, errors = {}, []
    with open(path, encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        need = {"entity_id", "date", "action"}
        if not need <= set(rd.fieldnames or []):
            sys.exit(f"Predictions must have columns {sorted(need)} (+ hours). Got {rd.fieldnames}")
        for i, r in enumerate(rd, start=2):
            a = (r.get("action") or "").strip().upper()
            if a not in ("SUGGEST", "NO_ACTION", "FLAG"):
                errors.append(f"line {i}: action must be SUGGEST, NO_ACTION or FLAG")
                continue
            h = norm(r.get("hours"))
            if a == "SUGGEST" and not HOURS_RE.match(h):
                errors.append(f"line {i}: invalid hours {r.get('hours')!r}")
                continue
            k = (r["entity_id"].strip(), r["date"].strip())
            if k in preds:
                errors.append(f"line {i}: duplicate row for {k}")
                continue
            preds[k] = (a, h, r.get("confidence"))
    if errors:
        sys.exit("Invalid predictions:\n  " + "\n  ".join(errors[:20]) + (f"\n  … {len(errors) - 20} more" if
                                                                            len(errors) > 20 else ""))
    return preds


def score(preds, labels_path):
    with open(labels_path, encoding="utf-8") as f:
        labels = list(csv.DictReader(f))
    label_keys = {(r["entity_id"], r["date"]) for r in labels}
    unknown = [k for k in preds if k not in label_keys]
    pts, mx = 0.0, 0
    c = Counter()
    contacted = defaultdict(int)
    views_saved = views_at_risk = 0.0
    vis_saved = vis_at_risk = 0.0
    for r in labels:
        k = (r["entity_id"], r["date"])
        a, h, _ = preds.get(k, ("NO_ACTION", "", None))
        exp, truth, shown = r["expected_action"], r["truth_hours"], r["displayed_hours_at_snapshot"]
        v = float(r.get("expected_search_views") or 0)
        if a == "SUGGEST":
            contacted[r["entity_id"]] += 1
        if exp == "SUGGEST":
            mx += 1
            vis_at_risk += v
            if r["mismatch_type"] == "SHOWN_OPEN_ACTUALLY_CLOSED":
                views_at_risk += v
            if a == "SUGGEST":
                if h == truth:
                    p, c["exact"] = 1.0, c["exact"] + 1
                elif (h == "closed") != (truth == "closed"):
                    p, c["harmful_wrong_state"] = -1.0, c["harmful_wrong_state"] + 1
                elif near(h, truth):
                    p, c["near"] = 0.6, c["near"] + 1
                else:
                    p, c["same_state_off"] = 0.2, c["same_state_off"] + 1
                if p > 0.5 and r["mismatch_type"] == "SHOWN_OPEN_ACTUALLY_CLOSED":
                    views_saved += v
                if p > 0.5:
                    vis_saved += v
            elif a == "FLAG":
                p, c["flag_on_change"] = 0.05, c["flag_on_change"] + 1
            else:
                p, c["missed"] = 0.0, c["missed"] + 1
        elif exp == "FLAG":
            mx += 1
            if a == "FLAG":
                p, c["flag_correct"] = 1.0, c["flag_correct"] + 1
            elif a == "SUGGEST":
                p = 0.3 if h == "closed" else -1.0
                c["suggest_to_closed_business"] += 1
            else:
                p, c["missed_closed_business"] = 0.0, c["missed_closed_business"] + 1
        else:  # NO_ACTION expected
            if a == "SUGGEST":
                if h == shown:
                    p, c["redundant_confirm"] = -0.1, c["redundant_confirm"] + 1
                elif (h == "closed") != (shown == "closed"):
                    p, c["harmful_wrong_state"] = -1.0, c["harmful_wrong_state"] + 1
                else:
                    p, c["spam_wrong_hours"] = -0.5, c["spam_wrong_hours"] + 1
            elif a == "FLAG":
                p, c["unneeded_flag"] = -0.1, c["unneeded_flag"] + 1
            else:
                p, c["correct_silence"] = 0.0, c["correct_silence"] + 1
        pts += p
    n_sug = sum(1 for k, (a, _, _) in preds.items() if a == "SUGGEST" and k in label_keys)
    res = {
        "score_0_100": round(max(0.0, 100 * pts / mx), 2) if mx else 0.0,
        "rows": len(labels), "rows_needing_action": mx,
        "suggestions_made": n_sug,
        "precision_exact": round(c["exact"] / n_sug, 3) if n_sug else 0.0,
        "recall_exact": round(c["exact"] / max(1, sum(1 for r in labels if r["expected_action"] == "SUGGEST")), 3),
        "harmful_suggestions": c["harmful_wrong_state"],
        "search_views_protected_pct": round(100 * vis_saved / vis_at_risk, 1) if vis_at_risk else 0.0,
        "closed_door_views_prevented_pct": round(100 * views_saved / views_at_risk, 1) if views_at_risk else 0.0,
        "businesses_contacted": len(contacted),
        "avg_dates_per_contacted_business": round(sum(contacted.values()) / len(contacted), 2) if contacted else 0,
        "breakdown": dict(c),
        "ignored_prediction_rows_not_in_labels": len(unknown),
    }
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--predictions", required=True)
    ap.add_argument("--labels", default=os.path.join(ROOT, "labels", "dev_labels_2026.csv"))
    ap.add_argument("--json", action="store_true", help="print JSON only")
    a = ap.parse_args()
    preds = load_predictions(a.predictions)
    res = score(preds, a.labels)
    matched = len(preds) - res["ignored_prediction_rows_not_in_labels"]
    if preds and matched == 0:
        print(f"WARNING: none of your {len(preds)} prediction rows match {os.path.basename(a.labels)} "
              "(wrong season or split?)", file=sys.stderr)
    if a.json:
        print(json.dumps(res, indent=2))
        return
    print(f"\nScore: {res['score_0_100']} / 100   ({os.path.basename(a.labels)})")
    for k in ("rows", "rows_needing_action", "suggestions_made", "precision_exact", "recall_exact",
              "harmful_suggestions", "search_views_protected_pct", "closed_door_views_prevented_pct", "businesses_contacted",
              "avg_dates_per_contacted_business", "ignored_prediction_rows_not_in_labels"):
        print(f"  {k:36s} {res[k]}")
    print("  breakdown:", json.dumps(res["breakdown"]))


if __name__ == "__main__":
    main()
