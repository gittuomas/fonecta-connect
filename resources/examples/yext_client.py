"""Minimal Yext Management API client (standard library only).

Works against the local mock and the real Yext sandbox without code changes:

    export YEXT_BASE_URL=http://localhost:8787            # mock (default)
    export YEXT_BASE_URL=https://sbx-api.yextapis.com     # your team's Yext sandbox
    export YEXT_API_KEY=...                               # never commit real keys
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def hours_to_yext_day(s: str) -> dict:
    """'closed' | '10:00-15:00' | '10:00-14:00,17:00-22:00' -> Yext day/holiday object."""
    s = s.strip().lower()
    if s == "closed":
        return {"isClosed": True}
    return {"openIntervals": [{"start": a, "end": b} for a, b in (iv.split("-") for iv in s.split(","))]}


class YextClient:
    def __init__(self, base_url=None, api_key=None, v="20261101", account="me"):
        self.base = (base_url or os.environ.get("YEXT_BASE_URL", "http://localhost:8787")).rstrip("/")
        self.key = api_key or os.environ.get("YEXT_API_KEY", "demo")
        self.v, self.account = v, account

    def _req(self, method, path, params=None, body=None):
        q = {"v": self.v, **(params or {})}
        url = f"{self.base}/v2/accounts/{self.account}/{path}?{urllib.parse.urlencode(q)}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"api-key": self.key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()[:400]}") from None

    # -- Knowledge Graph
    def list_entities(self, limit=50, filter_=None):
        token = None
        while True:
            p = {"limit": limit}
            if token:
                p["pageToken"] = token
            if filter_:
                p["filter"] = json.dumps(filter_)
            r = self._req("GET", "entities", p)["response"]
            yield from r.get("entities", [])
            token = r.get("pageToken")
            if not token:
                break

    def get_entity(self, entity_id):
        return self._req("GET", f"entities/{entity_id}")["response"]

    def set_holiday_hours(self, entity_id, day: str, hours: str):
        """Read-modify-write the full hours object with one holidayHours date (safe on the real API)."""
        hours_obj = self.get_entity(entity_id).get("hours", {})
        hol = [h for h in hours_obj.get("holidayHours", []) if h.get("date") != day]
        hol.append({"date": day, **hours_to_yext_day(hours)})
        hours_obj["holidayHours"] = sorted(hol, key=lambda h: h["date"])
        return self._req("PUT", f"entities/{entity_id}", body={"hours": hours_obj})

    # -- Reviews / Listings / Analytics
    def reviews(self, location_ids, min_date=None, limit=100):
        p = {"locationIds": ",".join(location_ids), "limit": limit}
        if min_date:
            p["minPublisherDate"] = min_date
        return self._req("GET", "reviews", p)["response"]

    def publisher_suggestions(self, location_ids, statuses=None):
        p = {"locationIds": ",".join(location_ids), "limit": 50}
        if statuses:
            p["statuses"] = ",".join(statuses)
        return self._req("GET", "listings/publishersuggestions", p)["response"]

    def listings(self, location_ids):
        return self._req("GET", "listings/listings", {"locationIds": ",".join(location_ids)})["response"]

    def analytics(self, metrics, start, end, entity_ids=None):
        body = {"metrics": metrics, "dimensions": ["DAYS", "ENTITY_IDS"],
                "filters": {"startDate": start, "endDate": end, **({"entityIds": entity_ids} if entity_ids else {})}}
        return self._req("POST", "analytics/reports", body=body)["response"]


def owner_response(entity_id, day, proposed_hours, channel=None, base_url=None):
    """Ask the local owner simulator how this business owner would react (mock only)."""
    base = (base_url or os.environ.get("YEXT_BASE_URL", "http://localhost:8787")).rstrip("/")
    body = {"entityId": entity_id, "date": day, "proposedHours": proposed_hours}
    if channel:
        body["channel"] = channel
    req = urllib.request.Request(f"{base}/sim/v1/owner-response", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())["response"]


if __name__ == "__main__":
    c = YextClient()
    e = c.get_entity("FI-D0001")
    print(e["name"], json.dumps(e["hours"], ensure_ascii=False)[:300])
