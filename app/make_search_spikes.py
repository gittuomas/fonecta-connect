#!/usr/bin/env python3
"""Precompute search spikes for the demo businesses, so the owner page itself stays standard-library only.

    python3 app/make_search_spikes.py      # needs pandas + pyarrow; writes app/search_spikes.json

For each demo business and target date: Google search views on the same date last year, divided by the
business's normal day (median of the 60 to 7 days before). 3.0 = three times the usual searches.
Used by the contact timeline: a message is urgent (AI call) when Google is wrong about open/closed on a day
that usually gets many searches.
"""
import csv
import json
import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
MAT = os.path.join(os.path.dirname(HERE), "resources")

a = pd.read_parquet(os.path.join(MAT, "data", "analytics_daily.parquet"),
                    columns=["date", "entity_id", "GOOGLE_SEARCH_VIEWS"])
a = a[a.entity_id.str.contains("-D00")]
a["date"] = pd.to_datetime(a["date"])
out = {}
with open(os.path.join(MAT, "evaluation", "targets_2026.csv"), encoding="utf-8") as f:
    for r in csv.DictReader(f):
        e = r["entity_id"]
        if "-D00" not in e:
            continue
        day = pd.Timestamp(r["date"]) - pd.DateOffset(years=1)
        x = a[a.entity_id == e]
        base = x[(x.date >= day - pd.Timedelta(days=60)) & (x.date < day - pd.Timedelta(days=7))]
        v = x[x.date == day]
        if len(v) and len(base):
            out[f"{e}|{r['date']}"] = round(float(v.GOOGLE_SEARCH_VIEWS.iloc[0]) / max(1.0, float(base.GOOGLE_SEARCH_VIEWS.median())), 2)
with open(os.path.join(HERE, "search_spikes.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, indent=0, sort_keys=True)
print(f"wrote {len(out)} values to app/search_spikes.json")
