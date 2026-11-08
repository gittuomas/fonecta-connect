"""Integration-readiness tests for step 5: the JSON contract, repeat safety, concurrency, the Python API and the
HTTP service that steps 1–4 will call.

    python -m unittest discover -s step5_apply/tests -t . -v      # from the repository root
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from step5_apply import confirm as C
from step5_apply import hours as H
from step5_apply import special_days as S
from step5_apply.api import Step5
from step5_apply.apply import (ALREADY_SET, APPLIED, CONFLICT, DECLINED, NO_RESPONSE, UNDONE, UNSET, Applier,
                                    Decision, DecisionItem)
from step5_apply.service import serve
from step5_apply.store import AuditLog, StandingPermissions

TODAY = date(2026, 11, 6)


class Contract(unittest.TestCase):
    def test_full_form(self):
        d = Decision.from_dict({"entity_id": "FI-D0001", "suggestion_id": "m1", "channel": "WHATSAPP",
                                "remember_yearly": True, "language": "en",
                                "items": [{"date": "2026-12-24", "hours": "closed", "decision": "approved",
                                           "seen_before": None},
                                          {"date": "2026-12-31", "hours": " 10:00 – 14:00", "decision": "EDIT"}]})
        self.assertEqual(d.items[0].decision, "ACCEPTED")
        self.assertIsNone(d.items[0].seen_before)
        self.assertEqual(d.items[1].decision, "EDITED")
        self.assertEqual(d.items[1].hours, "10:00-14:00")
        self.assertIs(d.items[1].seen_before, UNSET)          # absent = no conflict check
        self.assertTrue(d.remember_yearly)

    def test_single_date_shorthand_and_simulator_shape(self):
        d = Decision.from_dict({"entityId": "FI-D0001", "date": "2026-12-24", "decision": "EDITED",
                                "final_hours": "09:00-13:00"})
        self.assertEqual((d.entity_id, d.items[0].hours, d.items[0].decision), ("FI-D0001", "09:00-13:00", "EDITED"))

    def test_yext_hours_objects_are_accepted(self):
        d = Decision.from_dict({"entity_id": "X", "items": [
            {"date": "2026-12-24", "hours": {"isClosed": True}, "seen_before": {"isRegularHours": True}},
            {"date": "2026-12-31", "hours": {"openIntervals": [{"start": "10:00", "end": "14:00"}]}}]})
        self.assertEqual([i.hours for i in d.items], ["closed", "10:00-14:00"])
        self.assertEqual(d.items[0].seen_before, "regular")

    def test_declines_need_no_hours(self):
        d = Decision.from_dict({"entity_id": "X", "items": [{"date": "2026-12-24", "decision": "DECLINED"},
                                                            {"date": "2026-12-25", "decision": "timeout"}]})
        self.assertEqual([i.decision for i in d.items], ["REJECTED", "NO_RESPONSE"])

    def test_bad_input_raises_value_error_with_a_clear_message(self):
        for bad, needle in [({}, "entity_id"), ({"entity_id": "X", "items": []}, "non-empty"),
                            ({"entity_id": "X", "items": [{"hours": "closed"}]}, "date"),
                            ({"entity_id": "X", "date": "2026-12-24", "decision": "maybe"}, "unknown decision"),
                            ({"entity_id": "X", "date": "2026-12-24", "actor": "robot"}, "actor"),
                            ({"entity_id": "X", "items": [{"date": "2026-12-24"}, {"date": "2026-12-24"}]}, "once")]:
            with self.assertRaises(ValueError) as e:
                Decision.from_dict(bad)
            self.assertIn(needle, str(e.exception))

    def test_day_key_lookup(self):
        self.assertEqual(S.day_key("FI-D0001", "2026-12-24"), "XMAS_EVE")
        self.assertEqual(S.day_key("SE-D0008", "2026-12-13"), "LUCIA")
        self.assertEqual(S.day_key("FI-D0002", "2026-11-07"), "EVENT")
        self.assertEqual(S.day_key("FI-D0001", "2026-12-24", None), "XMAS_EVE")
        self.assertEqual(S.day_key("FI-D0001", "2026-12-02"), "")

    def test_undo_words(self):
        for yes in ("PERU", "peru!", "Ångra", "angre", "undo", "UNDO please"):
            self.assertTrue(C.is_undo_reply(yes), yes)
        for no in ("", "ok", "kiitos", "I said we are closed, don't undo anything else ever again"):
            self.assertFalse(C.is_undo_reply(no), no)


class Step5Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.s5 = Step5.from_env(use_sandbox=False, runtime_dir=cls.tmp, today=TODAY)

    @classmethod
    def tearDownClass(cls):
        cls.s5.close()

    def setUp(self):
        self.s5.mock.reset()
        t = Path(tempfile.mkdtemp())
        self.s5.applier.audit = AuditLog(t / "audit.jsonl")
        self.s5.applier.permissions = StandingPermissions(t / "perms.json")

    def puts(self):
        return len(self.s5.mock.module.STORE.update_log)

    def test_second_undo_reply_never_rolls_back_an_older_change(self):
        self.s5.apply({"entity_id": "FI-D0001", "date": "2026-12-24", "hours": "closed"})
        self.s5.apply({"entity_id": "FI-D0001", "date": "2026-12-31", "hours": "10:00-14:00"})
        self.s5.handle_reply("FI-D0001", "PERU")
        again = self.s5.handle_reply("FI-D0001", "PERU")
        self.assertEqual(again["reason"], "nothing to undo")
        self.assertEqual(self.s5.snapshot("FI-D0001", ["2026-12-24", "2026-12-31"]),
                         {"2026-12-24": "closed", "2026-12-31": None})

    def test_round_trip_snapshot_apply_reply_undo(self):
        seen = self.s5.snapshot("FI-D0001", ["2026-12-24"])
        self.assertEqual(seen, {"2026-12-24": None})
        res = self.s5.apply({"entity_id": "FI-D0001", "suggestion_id": "rt-1",
                             "items": [{"date": "2026-12-24", "hours": "closed", "seen_before": seen["2026-12-24"]}]})
        self.assertEqual(res["applied"], ["2026-12-24"])
        self.assertTrue(res["message"])
        json.dumps(res)                                         # the result must be JSON-serialisable
        self.assertIsNone(self.s5.handle_reply("FI-D0001", "kiitos!"))
        u = self.s5.handle_reply("FI-D0001", "PERU")
        self.assertEqual([i["status"] for i in u["items"]], [UNDONE])
        self.assertEqual(self.s5.snapshot("FI-D0001", ["2026-12-24"]), {"2026-12-24": None})
        self.assertEqual(self.s5.undo(entity_id="FI-D0001")["reason"], "nothing to undo")

    def test_double_tap_is_safe(self):
        dec = {"entity_id": "FI-D0003", "suggestion_id": "dbl-1",
               "items": [{"date": "2026-12-24", "hours": "closed", "seen_before": None}]}
        first = self.s5.apply(dec)
        n = self.puts()
        second = self.s5.apply(dec)
        self.assertEqual(self.puts(), n)                         # no second write
        self.assertEqual(second["items"][0]["status"], ALREADY_SET)
        self.assertTrue(second["items"][0]["duplicate"])
        self.assertIsNone(second["message"])                     # don't message the owner twice
        self.assertEqual(second["audit_id"], first["audit_id"])  # undo still finds the original

    def test_same_value_from_a_different_suggestion_says_already_up_to_date(self):
        self.s5.apply({"entity_id": "FI-D0003", "suggestion_id": "a", "date": "2026-12-24", "hours": "closed"})
        res = self.s5.apply({"entity_id": "FI-D0003", "suggestion_id": "b", "date": "2026-12-24", "hours": "closed"})
        self.assertFalse(res["items"][0]["duplicate"])
        self.assertIn("already up to date", res["message"])

    def test_policy_reflects_crm_and_learning(self):
        p = self.s5.policy("FI-D0012", date_="2026-12-24", hours="closed")
        self.assertTrue(p["auto_apply"])
        self.assertEqual(p["day_key"], "XMAS_EVE")
        self.assertFalse(self.s5.policy("FI-D0001", date_="2026-12-24", hours="closed")["auto_apply"])
        self.s5.apply({"entity_id": "FI-D0001", "remember_yearly": True, "date": "2026-12-24", "hours": "closed"})
        self.assertTrue(self.s5.policy("FI-D0001", date_="2026-12-24", hours="closed")["auto_apply"])
        self.assertFalse(self.s5.policy("FI-D0001", date_="2026-12-24", hours="10:00-14:00")["auto_apply"])

    def test_day_key_is_filled_in_and_events_are_not_remembered_yearly(self):
        self.s5.apply({"entity_id": "FI-D0002", "remember_yearly": True,
                       "items": [{"date": "2026-11-07", "hours": "11:00-23:00"},
                                 {"date": "2026-12-24", "hours": "closed"}]})
        perms = self.s5.applier.permissions
        self.assertIsNone(perms.lookup("FI-D0002", "EVENT"))
        self.assertTrue(perms.covers("FI-D0002", "XMAS_EVE", "closed"))

    def test_mixed_answers_in_one_message(self):
        res = self.s5.apply({"entity_id": "FI-D0007", "items": [
            {"date": "2026-12-24", "hours": "closed", "decision": "ACCEPTED"},
            {"date": "2026-12-28", "hours": "closed", "decision": "REJECTED"},
            {"date": "2026-12-29", "hours": "closed", "decision": "NO_RESPONSE"},
            {"date": "2026-12-31", "hours": "10:00-14:00", "decision": "EDITED"}]})
        self.assertEqual([i["status"] for i in res["items"]], [APPLIED, DECLINED, NO_RESPONSE, APPLIED])
        hol = self.s5.snapshot("FI-D0007", ["2026-12-24", "2026-12-28", "2026-12-29", "2026-12-31"])
        self.assertEqual(hol, {"2026-12-24": "closed", "2026-12-28": None, "2026-12-29": None,
                               "2026-12-31": "10:00-14:00"})

    def test_conflict_via_api(self):
        seen = self.s5.snapshot("FI-D0001", ["2026-12-31"])["2026-12-31"]
        self.s5.apply({"entity_id": "FI-D0001", "date": "2026-12-31", "hours": "10:00-15:00"})  # owner did it
        res = self.s5.apply({"entity_id": "FI-D0001", "date": "2026-12-31", "hours": "closed", "seen_before": seen})
        self.assertEqual(res["items"][0]["status"], CONFLICT)


class SlowFake:
    """In-memory Yext whose reads are slow, so two unsynchronised read-modify-writes would lose an update."""

    def __init__(self):
        self.name = "slow"
        self.hours = {d: {"openIntervals": [{"start": "09:00", "end": "17:00"}]} for d in H.DAYS}
        self._g = threading.Lock()

    def get_entity(self, eid):
        with self._g:
            snap = json.loads(json.dumps(self.hours))
        time.sleep(0.15)
        return {"name": "Slow Café", "hours": snap}

    def update_entity(self, eid, fields):
        with self._g:
            self.hours = json.loads(json.dumps(fields["hours"]))
        return {"meta": {"uuid": "x", "errors": []}}


class Concurrency(unittest.TestCase):
    def test_two_answers_at_once_do_not_overwrite_each_other(self):
        tmp = Path(tempfile.mkdtemp())
        fake = SlowFake()
        a = Applier(fake, AuditLog(tmp / "a.jsonl"), StandingPermissions(tmp / "p.json"), {}, today=TODAY)
        results = []
        ts = [threading.Thread(target=lambda d=d: results.append(a.apply(Decision("X", [DecisionItem(d, "closed")]))))
              for d in ("2026-12-24", "2026-12-31")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(r.items[0].status for r in results), [APPLIED, APPLIED])
        self.assertEqual(sorted(H.holiday_map(fake.hours)), ["2026-12-24", "2026-12-31"])


class HttpService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.s5 = Step5.from_env(use_sandbox=False, runtime_dir=cls.tmp, today=TODAY)
        from step5_apply.service import Handler
        Handler.quiet = True
        cls.httpd = serve(cls.s5, port=0)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.s5.close()

    def setUp(self):
        self.s5.mock.reset()
        t = Path(tempfile.mkdtemp())
        self.s5.applier.audit = AuditLog(t / "audit.jsonl")
        self.s5.applier.permissions = StandingPermissions(t / "perms.json")

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode()), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode()), e.headers

    def test_health(self):
        code, body, headers = self.call("GET", "/health")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertIsNone(body["sandbox"])
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")

    def test_full_flow_over_http(self):
        code, snap, _ = self.call("GET", "/snapshot?entity_id=FI-D0001&dates=2026-12-24")
        self.assertEqual((code, snap["seen_before"]), (200, {"2026-12-24": None}))
        code, res, _ = self.call("POST", "/apply", {
            "entity_id": "FI-D0001", "suggestion_id": "http-1", "language": "en",
            "items": [{"date": "2026-12-24", "hours": "closed", "decision": "ACCEPTED", "seen_before": None}]})
        self.assertEqual(code, 200)
        self.assertEqual(res["applied"], ["2026-12-24"])
        self.assertIn("Google", res["message"])
        code, audit, _ = self.call("GET", "/audit?entity_id=FI-D0001")
        self.assertEqual([r["id"] for r in audit["records"]], [res["audit_id"]])
        code, no, _ = self.call("POST", "/reply", {"entity_id": "FI-D0001", "text": "thanks"})
        self.assertEqual(no, {"undo": False})
        code, u, _ = self.call("POST", "/reply", {"entity_id": "FI-D0001", "text": "UNDO"})
        self.assertEqual([i["status"] for i in u["items"]], [UNDONE])

    def test_undo_by_audit_id(self):
        _, res, _ = self.call("POST", "/apply", {"entity_id": "FI-D0003", "date": "2026-12-24", "hours": "closed"})
        code, u, _ = self.call("POST", "/undo", {"audit_id": res["audit_id"]})
        self.assertEqual((code, u["items"][0]["status"]), (200, UNDONE))
        code, err, _ = self.call("POST", "/undo", {"audit_id": "nope"})
        self.assertEqual(code, 400)

    def test_policy_and_where(self):
        _, p, _ = self.call("GET", "/policy?entity_id=FI-D0012&date=2026-12-24&hours=closed")
        self.assertTrue(p["auto_apply"])
        _, w, _ = self.call("GET", "/where?entity_id=FI-D0001")
        self.assertEqual(w["publishers"][0]["publisherId"], "GOOGLEMYBUSINESS")

    def test_errors_are_json_400_404(self):
        self.assertEqual(self.call("POST", "/apply", raw=b"{not json")[0], 400)
        code, body, _ = self.call("POST", "/apply", {"entity_id": "FI-D0001", "items": []})
        self.assertEqual(code, 400)
        self.assertIn("non-empty", body["error"])
        self.assertEqual(self.call("GET", "/snapshot?entity_id=FI-D0001")[0], 400)
        self.assertEqual(self.call("GET", "/nope")[0], 400)       # missing entity_id is checked first
        self.assertEqual(self.call("GET", "/nope?entity_id=X")[0], 404)
        self.assertEqual(self.call("POST", "/nope", {})[0], 404)

    def test_unknown_business_is_a_failed_item_not_a_crash(self):
        code, res, _ = self.call("POST", "/apply", {"entity_id": "FI-NOPE", "date": "2026-12-24", "hours": "closed"})
        self.assertEqual(code, 200)
        self.assertEqual(res["items"][0]["status"], "FAILED")
        self.assertIn("read failed", res["items"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
