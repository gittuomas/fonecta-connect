#!/usr/bin/env python3
"""Local mock of the Yext Management API (v2) backed by the synthetic dataset, plus an owner simulator.

    python mock_api/server.py            # http://localhost:8787
    python mock_api/server.py --port 9000

Same URL shapes as Yext, so the same client code works against the real sandbox:
    mock:     http://localhost:8787/v2/accounts/me/entities?api_key=demo&v=20261101
    sandbox:  https://sbx-api.yextapis.com/v2/accounts/me/entities?api_key=<team key>&v=20261101

Only the Python standard library is needed (pandas + pyarrow only for the analytics endpoint).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import threading
import uuid
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from hours import DAYS, is_valid_hours, yext_day_to_str  # noqa: E402
import simulator  # noqa: E402

ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
STATE_FILE = os.path.join(HERE, "state.json")
LOCK = threading.Lock()


def load_json(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return json.load(f)


class Store:
    def __init__(self):
        ents = load_json("entities.json")["response"]["entities"]
        self.base = {e["meta"]["id"]: e for e in ents}
        self.order = [e["meta"]["id"] for e in ents]
        self.overrides = {}
        self.update_log = []
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, encoding="utf-8") as f:
                st = json.load(f)
            self.overrides, self.update_log = st.get("overrides", {}), st.get("update_log", [])
        self.reviews = load_json("reviews.json")["response"]["reviews"]
        self.suggestions = load_json("publisher_suggestions.json")["response"]["suggestions"]
        self.listings = load_json("listings.json")["listings"]
        self.analytics = None
        sim_path = os.path.join(HERE, "sim_data.json")
        self.sim = json.load(open(sim_path, encoding="utf-8")) if os.path.exists(sim_path) else {"personas": {},
                                                                                                  "truth": {}}
        self.dev_truth = {}
        for fn, col in (("dev_truth_history.csv", "truth_hours"), ("dev_labels_2026.csv", "truth_hours"),
                        ("dev_labels_2025_backtest.csv", "truth_hours")):
            p = os.path.join(ROOT, "labels", fn)
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        self.dev_truth[f"{r['entity_id']}|{r['date']}"] = r[col]

    def entity(self, eid):
        if eid not in self.base:
            return None
        e = json.loads(json.dumps(self.base[eid]))
        e.update(self.overrides.get(eid, {}))
        return e

    def save(self):
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"overrides": self.overrides, "update_log": self.update_log}, f, ensure_ascii=False)

    def get_analytics(self):
        if self.analytics is None:
            import pandas as pd  # optional dependency
            self.analytics = pd.read_parquet(os.path.join(DATA, "analytics_daily.parquet"))
            self.analytics["date"] = self.analytics["date"].astype(str)
        return self.analytics


STORE: Store | None = None


def _interval_errors(where, d):
    errs = []
    if not isinstance(d, dict):
        return [f"{where}: must be an object like {{\"openIntervals\":[{{\"start\":\"09:00\",\"end\":\"17:00\"}}]}}"]
    ivs = d.get("openIntervals") or []
    if not isinstance(ivs, list):
        return [f"{where}: openIntervals must be a list"]
    spans = []
    for iv in ivs:
        if not isinstance(iv, dict) or not {"start", "end"} <= set(iv):
            errs.append(f"{where}: each interval needs start and end")
            continue
        s, e = str(iv["start"]), str(iv["end"])
        if not (re.match(r"^([01]\d|2[0-3]):[0-5]\d$", s) and re.match(r"^([01]\d|2[0-3]):[0-5]\d$", e)):
            errs.append(f"{where}: times must be HH:MM ({s}-{e})")
            continue
        if s == e:
            errs.append(f"{where}: start equals end ({s}); use 00:00-23:59 for 24 hours")
            continue
        a, b = int(s[:2]) * 60 + int(s[3:]), int(e[:2]) * 60 + int(e[3:])
        spans.append((a, b if b > a else b + 1440))
    spans.sort()
    for (a1, b1), (a2, b2) in zip(spans, spans[1:]):
        if a2 < b1:
            errs.append(f"{where}: intervals overlap")
    if d.get("isClosed") and ivs:
        errs.append(f"{where}: cannot be closed and have openIntervals")
    return errs


def validate_hours(h: dict) -> list[str]:
    errs = []
    if not isinstance(h, dict):
        return ["hours must be an object"]
    days_given = [k for k in h if k in DAYS]
    if days_given and len(days_given) != 7:
        errs.append("hours: send all seven weekdays (read-modify-write the full hours object)")
    for k, v in h.items():
        if k in DAYS:
            errs += _interval_errors(f"hours.{k}", v)
        elif k == "holidayHours":
            if not isinstance(v, list):
                errs.append("holidayHours must be a list")
                continue
            seen = set()
            for item in v:
                if not isinstance(item, dict):
                    errs.append("holidayHours: items must be objects")
                    continue
                d = str(item.get("date", ""))
                try:
                    date.fromisoformat(d)
                except ValueError:
                    errs.append(f"holidayHours: bad date {d!r}")
                    continue
                if d in seen:
                    errs.append(f"holidayHours: duplicate date {d}")
                seen.add(d)
                flags = sum(bool(item.get(x)) for x in ("isClosed", "isRegularHours")) + bool(item.get("openIntervals"))
                if flags != 1:
                    errs.append(f"holidayHours {d}: set exactly one of isClosed / isRegularHours / openIntervals")
                elif item.get("openIntervals"):
                    errs += _interval_errors(f"holidayHours {d}", item)
        elif k in ("reopenDate", "holidayHoursConversationEnabled"):
            continue
        else:
            errs.append(f"hours: unknown key {k}")
    return errs


class Handler(BaseHTTPRequestHandler):
    server_version = "YextMock/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("MOCK_QUIET") != "1":
            sys.stderr.write("[mock] " + fmt % args + "\n")

    # --------------------------------------------------------------- utils
    def _send(self, code, response=None, errors=None):
        body = {"meta": {"uuid": str(uuid.uuid4()), "errors": errors or []}, "response": response or {}}
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def _err(self, code, msg, ecode=None):
        self._send(code, {}, [{"code": ecode or code, "type": "FATAL_ERROR", "message": msg}])

    def _auth(self, q):
        key = (q.get("api_key") or [None])[0] or self.headers.get("api-key")
        if not key:
            self._err(401, "Missing api_key (query) or api-key (header).", 1)
            return False
        v = (q.get("v") or [None])[0]
        if not v or not re.match(r"^\d{8}$", v):
            self._err(400, "Missing or invalid v parameter (YYYYMMDD).", 2)
            return False
        return True

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except json.JSONDecodeError:
            return None

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, api-key, Yext-User-Id")
        self.end_headers()

    # --------------------------------------------------------------- routing
    def _safe(self, fn):
        try:
            fn()
        except (ValueError, TypeError, KeyError) as ex:
            self._err(400, f"Bad request: {ex}")
        except Exception as ex:  # never drop the connection
            self._err(500, f"Mock error: {ex}")

    def do_GET(self):
        self._safe(self._get)

    def do_PUT(self):
        self._safe(self._put)

    def do_POST(self):
        self._safe(self._post)

    def _get(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path.rstrip("/")
        if p in ("", "/health"):
            return self._send(200, {"status": "ok", "entities": len(STORE.base),
                                    "docs": "See START_HERE.md › Mock API"})
        if not self._auth(q):
            return
        m = re.match(r"^/v2/accounts/([^/]+)/entities(?:/([^/]+))?$", p)
        if m:
            return self.entities_get(m.group(2), q)
        if re.match(r"^/v2/accounts/[^/]+/reviews$", p):
            return self.reviews(q)
        if re.match(r"^/v2/accounts/[^/]+/listings/publishersuggestions$", p):
            return self.pub_suggestions(q)
        if re.match(r"^/v2/accounts/[^/]+/listings/listings$", p):
            return self.listings(q)
        self._err(404, f"Unknown endpoint {p}")

    def _put(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._auth(q):
            return
        m = re.match(r"^/v2/accounts/[^/]+/entities/([^/]+)$", u.path.rstrip("/"))
        if not m:
            return self._err(404, "Unknown endpoint")
        self.entity_put(m.group(1), self._body())

    def _post(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path.rstrip("/")
        if p == "/sim/v1/owner-response":
            return self.owner_response(self._body())
        if p == "/mock/reset":
            with LOCK:
                STORE.overrides, STORE.update_log = {}, []
                STORE.save()
            return self._send(200, {"reset": True})
        if not self._auth(q):
            return
        if re.match(r"^/v2/accounts/[^/]+/analytics/reports$", p):
            return self.analytics(self._body() or {})
        self._err(404, f"Unknown endpoint {p}")

    # --------------------------------------------------------------- handlers
    def entities_get(self, eid, q):
        if eid:
            e = STORE.entity(eid)
            return self._send(200, e) if e else self._err(404, f"Entity {eid} not found", 2000)
        limit = min(int((q.get("limit") or [10])[0]), 50)
        start = int((q.get("pageToken") or [0])[0] or 0)
        ids = STORE.order
        types = (q.get("entityTypes") or [None])[0]
        flt = (q.get("filter") or [None])[0]
        conds = {}
        if flt:
            try:
                for k, v in json.loads(flt).items():
                    if isinstance(v, dict) and "$in" in v:
                        conds[k] = ("$in", list(v["$in"]))
                    elif isinstance(v, dict) and "$eq" in v:
                        conds[k] = v["$eq"]
                    elif isinstance(v, dict):
                        raise ValueError(f"filter operator {list(v)} not supported by the mock (use $eq or $in)")
                    else:
                        conds[k] = v
            except (json.JSONDecodeError, AttributeError):
                return self._err(400, "filter must be JSON like {\"meta.countryCode\":{\"$eq\":\"FI\"}}")
            except ValueError as ex:
                return self._err(400, str(ex))
        if types or conds:
            sel = []
            for i in ids:
                e = STORE.base[i]
                if types and e["meta"]["entityType"] not in types.split(","):
                    continue
                ok = True
                for k, v in conds.items():
                    cur = e
                    for part in k.split("."):
                        cur = cur.get(part) if isinstance(cur, dict) else None
                    ok &= (cur in v[1]) if isinstance(v, tuple) else (cur == v)
                if ok:
                    sel.append(i)
            ids = sel
        page = [STORE.entity(i) for i in ids[start:start + limit]]
        resp = {"count": len(ids), "entities": page}
        if start + limit < len(ids):
            resp["pageToken"] = str(start + limit)
        self._send(200, resp)

    def entity_put(self, eid, body):
        if body is None:
            return self._err(400, "Body must be JSON")
        cur = STORE.entity(eid)
        if not cur:
            return self._err(404, f"Entity {eid} not found", 2000)
        warnings = []
        upd = {}
        if "hours" in body:
            h = body["hours"]
            errs = validate_hours(h)
            if errs:
                return self._err(400, "; ".join(errs), 2201)
            if not any(d in h for d in DAYS):
                merged = dict(cur.get("hours", {}))
                merged["holidayHours"] = h.get("holidayHours", [])
                h = merged
                warnings.append({"code": 9001, "type": "WARNING",
                                 "message": "Only holidayHours sent; weekday hours were kept. Send the full hours "
                                            "object (read-modify-write) to be safe on the real API."})
            upd["hours"] = h
        for k in ("closed", "name"):
            if k in body:
                upd[k] = body[k]
        unknown = [k for k in body if k not in ("hours", "closed", "name")]
        if unknown:
            return self._err(400, f"Mock supports only hours/closed/name updates, got {unknown}")
        with LOCK:
            STORE.overrides.setdefault(eid, {}).update(upd)
            STORE.update_log.append({"entityId": eid, "at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                                     "fields": list(upd)})
            STORE.save()
        e = STORE.entity(eid)
        body = {"meta": {"uuid": str(uuid.uuid4()), "errors": warnings}, "response": {"id": eid, **e}}
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def reviews(self, q):
        locs = set(",".join(q.get("locationIds", [])).split(",")) - {""}
        mn = (q.get("minPublisherDate") or [None])[0]
        mx = (q.get("maxPublisherDate") or [None])[0]
        limit = min(int((q.get("limit") or [10])[0]), 100)
        off = int((q.get("offset") or [0])[0])
        to_ms = lambda s: int(datetime.fromisoformat(s).timestamp() * 1000)  # noqa: E731
        sel = [r for r in STORE.reviews if (not locs or r["locationId"] in locs)
               and (not mn or r["publisherDate"] >= to_ms(mn)) and (not mx or r["publisherDate"] < to_ms(mx) + 86400000)]
        self._send(200, {"count": len(sel), "reviews": sel[off:off + limit]})

    def pub_suggestions(self, q):
        locs = set(",".join(q.get("locationIds", [])).split(",")) - {""}
        sts = set(",".join(q.get("statuses", [])).split(",")) - {""}
        limit = min(int((q.get("limit") or [10])[0]), 50)
        off = int((q.get("offset") or [0])[0])
        sel = [s for s in STORE.suggestions if (not locs or s["locationId"] in locs) and (not sts or s["status"] in sts)]
        self._send(200, {"count": len(sel), "suggestions": sel[off:off + limit]})

    def listings(self, q):
        locs = set(",".join(q.get("locationIds", []) + q.get("entityIds", [])).split(",")) - {""}
        if not locs:
            return self._err(400, "locationIds is required")
        last = {}
        for u in STORE.update_log:
            last[u["entityId"]] = u["at"]
        out = []
        for l in STORE.listings:
            if l["entityId"] in locs:
                row = dict(l)
                if l["entityId"] in last and l["status"] == "LIVE":
                    row["lastUpdateSentAt"] = last[l["entityId"]]
                    t = datetime.fromisoformat(last[l["entityId"]].rstrip("Z"))
                    row["expectedLiveBy"] = datetime.fromtimestamp(t.timestamp() + l["typicalUpdateSpeedSeconds"]
                                                                   ).isoformat(timespec="seconds") + "Z"
                out.append(row)
        self._send(200, {"count": len(out), "listings": out})

    def analytics(self, body):
        try:
            df = STORE.get_analytics()
        except Exception as ex:  # pandas / pyarrow missing
            return self._err(501, f"Analytics needs pandas + pyarrow: {ex}")
        metrics = body.get("metrics") or ["GOOGLE_SEARCH_VIEWS"]
        bad = [m for m in metrics if m not in df.columns]
        if bad:
            return self._err(400, f"Unknown metrics {bad}. Available: {[c for c in df.columns if c.isupper()]}")
        f = body.get("filters", {})
        sel = df
        if f.get("startDate"):
            sel = sel[sel["date"] >= f["startDate"]]
        if f.get("endDate"):
            sel = sel[sel["date"] <= f["endDate"]]
        if f.get("entityIds"):
            sel = sel[sel["entity_id"].isin(f["entityIds"])]
        dims = body.get("dimensions") or ["DAYS"]
        keys = [{"DAYS": "date", "ENTITY_IDS": "entity_id"}[d] for d in dims if d in ("DAYS", "ENTITY_IDS")]
        if len(sel) > 200000 and "entity_id" in keys:
            return self._err(400, "Report too large; filter entityIds or dates (or read the parquet file directly).")
        g = sel.groupby(keys)[metrics].sum().reset_index() if keys else sel[metrics].sum().to_frame().T
        self._send(200, {"data": json.loads(g.to_json(orient="records"))})

    def owner_response(self, body):
        if not body:
            return self._err(400, "JSON body required")
        eid, d, prop = body.get("entityId"), body.get("date"), (body.get("proposedHours") or "").strip().lower()
        if not (eid and d and prop):
            return self._err(400, "entityId, date and proposedHours ('closed' or '10:00-15:00') are required")
        if not is_valid_hours(prop):
            return self._err(400, "proposedHours must be 'closed' or like '10:00-15:00' / '10:00-14:00,17:00-22:00'")
        persona = STORE.sim["personas"].get(eid)
        key = f"{eid}|{d}"
        truth = STORE.dev_truth.get(key) or STORE.sim["truth"].get(key)
        if not persona or truth is None:
            return self._err(403, "Simulator answers only for dev and demo entities on special days "
                                  "(test entities are used for scoring).")
        ch = body.get("channel") or persona["channel"]
        res = simulator.respond(truth, prop, persona["responsiveness"], ch, "mock", eid, d,
                                body.get("messageId", ""), send_hour=int(body.get("sendHour", 10)))
        res["channel"] = ch
        self._send(200, res)


def main():
    global STORE
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8787)))
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    STORE = Store()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"Yext mock running on http://{a.host}:{a.port}  ({len(STORE.base)} entities). Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
