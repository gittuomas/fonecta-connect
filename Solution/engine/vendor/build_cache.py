"""Build parquet caches used by common.py. Run once: python3 build_cache.py"""
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CACHE, D, DAYS, day_str  # noqa: E402

os.makedirs(CACHE, exist_ok=True)

# entities
ents = json.load(open(D("entities.json"), encoding="utf-8"))["response"]["entities"]
rows = []
for e in ents:
    h = e.get("hours", {})
    r = {
        "entity_id": e["meta"]["id"], "country": e["meta"]["countryCode"], "language": e["meta"]["language"],
        "entity_type": e["meta"].get("entityType"), "name": e.get("name"), "city": e.get("address", {}).get("city"),
        "lat": e.get("geocodedCoordinate", {}).get("latitude"), "lon": e.get("geocodedCoordinate", {}).get("longitude"),
        "category_id": (e.get("categoryIds") or [None])[0], "segment": e.get("c_segment"),
        "mall_id": e.get("c_shoppingCenterEntityId"), "entity_role": e.get("c_entityRole"),
        "connect_product": e.get("c_connectProduct"), "customer_since": e.get("c_connectCustomerSince"),
        "closed_flag": bool(e.get("closed")), "reopen_date": h.get("reopenDate"),
        "n_holiday_hours_entered": len(h.get("holidayHours", []) or []),
        "holiday_hours_json": json.dumps(h.get("holidayHours", []) or []),
    }
    for d in DAYS:
        r[d] = day_str(h.get(d)) if d in h else "closed"
    rows.append(r)
pd.DataFrame(rows).to_parquet(os.path.join(CACHE, "entities.parquet"), index=False)

# holiday history
rows = []
for line in open(D("holiday_hours_history.jsonl"), encoding="utf-8"):
    r = json.loads(line)
    rows.append({"entity_id": r["entity_id"], "date": r["date"], "day_key": r["day_key"],
                 "hours": day_str(r["holidayHours"]), "entered_at": r["entered_at"], "source": r["source"]})
pd.DataFrame(rows).to_parquet(os.path.join(CACHE, "holiday_hours_history.parquet"), index=False)

# regular history
rows = []
for line in open(D("regular_hours_history.jsonl"), encoding="utf-8"):
    r = json.loads(line)
    row = {"entity_id": r["entity_id"], "effective_from": r["effective_from"]}
    for d in DAYS:
        row[d] = day_str(r["hours"].get(d)) if d in r["hours"] else "closed"
    rows.append(row)
pd.DataFrame(rows).to_parquet(os.path.join(CACHE, "regular_hours_history.parquet"), index=False)

# reviews
rv = json.load(open(D("reviews.json"), encoding="utf-8"))["response"]["reviews"]
df = pd.DataFrame([{"id": r["id"], "locationId": r["locationId"], "rating": r["rating"], "content": r.get("content") or "",
                    "publisherDate": pd.to_datetime(r["publisherDate"], unit="ms"), "publisherId": r["publisherId"],
                    "authorName": r.get("authorName"), "n_owner_replies": len(r.get("comments") or [])} for r in rv])
df.to_parquet(os.path.join(CACHE, "reviews.parquet"), index=False)

# publisher suggestions
ps = json.load(open(D("publisher_suggestions.json"), encoding="utf-8"))["response"]["suggestions"]
pd.DataFrame(ps).to_parquet(os.path.join(CACHE, "publisher_suggestions.parquet"), index=False)
items = []
for s in ps:
    try:
        sc = json.loads(s["suggestedContent"]) if s.get("suggestedContent") else {}
    except Exception:
        sc = {}
    try:
        oc = json.loads(s["originalContent"]) if s.get("originalContent") else {}
    except Exception:
        oc = {}
    orig = {h.get("date"): day_str(h) for h in (oc.get("holidayHours") or [])} if isinstance(oc, dict) else {}
    hh = sc.get("holidayHours") if isinstance(sc, dict) else None
    base = {k: s.get(k) for k in ("id", "publisherId", "locationId", "dateCreated", "dateResolved", "fieldName", "status",
                                  "resolvedBy")}
    if hh:
        for h in hh:
            items.append({**base, "item_type": "holidayHours", "item_date": h.get("date"), "sugg_hours": day_str(h),
                          "orig_hours": orig.get(h.get("date")), "raw_suggested": s["suggestedContent"]})
    else:
        items.append({**base, "item_type": "other", "item_date": None, "sugg_hours": None, "orig_hours": None,
                      "raw_suggested": s.get("suggestedContent")})
pd.DataFrame(items).to_parquet(os.path.join(CACHE, "publisher_suggestion_items.parquet"), index=False)

# events
ev = json.load(open(D("local_events.json"), encoding="utf-8"))["data"]
pd.DataFrame([{"event_id": e["@id"], "name_en": (e.get("name") or {}).get("en"), "start_time": e.get("start_time"),
               "end_time": e.get("end_time"), "city": (e.get("location") or {}).get("address_locality"),
               "venue": ((e.get("location") or {}).get("name") or {}).get("en"),
               "lon": (e.get("location") or {}).get("position", {}).get("coordinates", [None, None])[0],
               "lat": (e.get("location") or {}).get("position", {}).get("coordinates", [None, None])[1],
               "keywords": ",".join(e.get("keywords") or []), "country": e.get("x_country"),
               "expected_attendance": e.get("x_expected_attendance")} for e in ev]).to_parquet(
    os.path.join(CACHE, "local_events.parquet"), index=False)

print("cache built:", sorted(os.listdir(CACHE)))
