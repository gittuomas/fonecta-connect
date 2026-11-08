"""Step 5 tests against the challenge's own Yext mock (in-process) plus a few fakes for failure paths.

    python -m unittest discover -s step5_apply/tests -t . -v      # from the repository root
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from datetime import date
from pathlib import Path
from unittest import mock

from step5_apply import confirm as C
from step5_apply import hours as H
from step5_apply.apply import (ALREADY_SET, APPLIED, BLOCKED, CONFLICT, DECLINED, FAILED, INVALID, NO_RESPONSE,
                                    UNDONE, Applier, Decision, DecisionItem)
from step5_apply.mock_runtime import start_mock
from step5_apply.store import AuditLog, StandingPermissions, load_contacts
from step5_apply.yext import YextClient, YextError

TODAY = date(2026, 11, 6)
CONTACTS = load_contacts()
OPTED_OUT = next(k for k, v in CONTACTS.items() if v["opted_out_of_suggestions"] == "1")


class MockTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = start_mock()

    @classmethod
    def tearDownClass(cls):
        cls.mock.stop()

    def setUp(self):
        self.mock.reset()
        self.tmp = Path(tempfile.mkdtemp())
        self.client = YextClient(self.mock.url, "demo", name="mock")
        self.audit = AuditLog(self.tmp / "audit.jsonl")
        self.perms = StandingPermissions(self.tmp / "perms.json")
        self.applier = Applier(self.client, self.audit, self.perms, CONTACTS, today=TODAY)

    def hol(self, eid):
        return H.holiday_map(self.client.get_entity(eid)["hours"])

    def puts(self):
        return len(self.mock.module.STORE.update_log)


class ApplyHappyPath(MockTestCase):
    def test_owner_accept_writes_verifies_and_keeps_everything_else(self):
        before = self.client.get_entity("FI-D0001")["hours"]
        res = self.applier.apply(Decision("FI-D0001", [DecisionItem("2026-12-24", "closed", "ACCEPTED", "XMAS_EVE",
                                                                    seen_before=None)]))
        self.assertEqual([i.status for i in res.items], [APPLIED])
        after = self.client.get_entity("FI-D0001")["hours"]
        self.assertEqual(H.holiday_map(after)["2026-12-24"], "closed")
        self.assertEqual(H.weekdays(after), H.weekdays(before))
        self.assertEqual(len(self.audit.all()), 1)
        self.assertIn("Google", res.message)
        self.assertIn("24 Dec closed", res.message)  # English by default (team decision for the demo)

    def test_edited_hours_are_what_gets_written(self):
        res = self.applier.apply(Decision("FI-D0001", [DecisionItem("2026-12-31", "10:00 – 14:00", "EDITED")]))
        self.assertEqual(res.items[0].status, APPLIED)
        self.assertEqual(self.hol("FI-D0001")["2026-12-31"], "10:00-14:00")

    def test_several_dates_go_in_one_put_and_existing_dates_survive(self):
        existing = self.hol("FI-D0013")                     # already has 5 holiday dates
        n = self.puts()
        res = self.applier.apply(Decision("FI-D0013", [DecisionItem("2026-12-26", "closed"),
                                                       DecisionItem("2026-12-31", "08:00-12:00")]))
        self.assertEqual([i.status for i in res.items], [APPLIED, APPLIED])
        self.assertEqual(self.puts() - n, 1)
        now = self.hol("FI-D0013")
        for d, h in existing.items():
            self.assertEqual(now[d], h)

    def test_regular_hours_marker(self):
        res = self.applier.apply(Decision("FI-D0001", [DecisionItem("2026-12-23", "regular")]))
        self.assertEqual(res.items[0].status, APPLIED)
        self.assertEqual(self.hol("FI-D0001")["2026-12-23"], "regular")


class Language(MockTestCase):
    def test_english_by_default_owner_language_on_request(self):
        en = self.applier.apply(Decision("FI-D0001", [DecisionItem("2026-12-24", "closed")]))
        self.assertTrue(en.message.startswith("Done"))
        self.mock.reset()
        fi = self.applier.apply(Decision("FI-D0001", [DecisionItem("2026-12-24", "closed")], language="owner"))
        self.assertIn("kiinni", fi.message)                    # FI-D0001's CRM language is Finnish
        self.mock.reset()
        sv = self.applier.apply(Decision("FI-D0001", [DecisionItem("2026-12-24", "closed")], language="sv"))
        self.assertIn("stängt", sv.message)

    def test_short_message_for_sms_and_whatsapp(self):
        res = self.applier.apply(Decision("FI-D0001", [DecisionItem("2026-12-24", "closed"),
                                                       DecisionItem("2026-12-31", "10:00-14:00")]))
        self.assertNotIn(chr(10), res.message_short)          # one line
        self.assertIn("Kahvila Aurora: 24 Dec closed, 31 Dec open 10–14", res.message_short)
        self.assertIn("Live on Google ~2 min + 8 more", res.message_short)
        self.assertIn("UNDO", res.message_short)
        self.assertLess(len(res.message_short), 160)


class Gate(MockTestCase):
    def test_declined_and_no_response_never_write(self):
        n = self.puts()
        res = self.applier.apply(Decision("FI-D0001", [DecisionItem("2026-12-24", "closed", "REJECTED"),
                                                       DecisionItem("2026-12-25", "closed", "NO_RESPONSE")]))
        self.assertEqual([i.status for i in res.items], [DECLINED, NO_RESPONSE])
        self.assertEqual(self.puts(), n)
        self.assertIsNone(res.message)
        self.assertEqual(self.audit.all(), [])

    def test_invalid_hours_and_past_dates_are_blocked(self):
        res = self.applier.apply(Decision("FI-D0001", [DecisionItem("2026-12-24", "10:00-10:00"),
                                                       DecisionItem("2026-12-24x", "closed"),
                                                       DecisionItem("2026-11-01", "closed"),
                                                       DecisionItem("2026-12-30", "09:00-13:00,12:00-15:00")]))
        self.assertEqual([i.status for i in res.items], [INVALID, INVALID, BLOCKED, INVALID])

    def test_opted_out_owner_is_never_written(self):
        res = self.applier.apply(Decision(OPTED_OUT, [DecisionItem("2026-12-24", "closed")]))
        self.assertEqual(res.items[0].status, BLOCKED)

    def test_standing_permission_needed_for_auto_apply(self):
        res = self.applier.apply(Decision("FI-D0001", [DecisionItem("2026-12-24", "closed", day_key="XMAS_EVE")],
                                          actor="standing_permission"))
        self.assertEqual(res.items[0].status, BLOCKED)

    def test_crm_standing_permission_applies_then_informs(self):
        res = self.applier.apply(Decision("FI-D0012", [DecisionItem("2026-12-24", "closed", day_key="XMAS_EVE")],
                                          actor="standing_permission"))
        self.assertEqual(res.items[0].status, APPLIED)
        self.assertIn("standing permission", res.message)


class ConflictsAndLearning(MockTestCase):
    def test_owner_changed_it_meanwhile_is_not_overwritten(self):
        # suggestion was sent when there was no entry; owner then set 10-14 themselves in the Connect app
        self.client.update_entity("FI-D0001", {"hours": H.with_holiday_changes(
            self.client.get_entity("FI-D0001")["hours"], {"2026-12-24": "10:00-14:00"})})
        res = self.applier.apply(Decision("FI-D0001", [DecisionItem("2026-12-24", "closed", seen_before=None)]))
        self.assertEqual(res.items[0].status, CONFLICT)
        self.assertEqual(self.hol("FI-D0001")["2026-12-24"], "10:00-14:00")

    def test_already_correct_is_a_no_op(self):
        n = self.puts()
        res = self.applier.apply(Decision("FI-D0013", [DecisionItem("2026-12-24", "closed")]))
        self.assertEqual(res.items[0].status, ALREADY_SET)
        self.assertEqual(self.puts(), n)

    def test_every_year_grants_standing_permission_for_that_day_only(self):
        self.applier.apply(Decision("FI-D0001", [DecisionItem("2026-12-24", "closed", day_key="XMAS_EVE")],
                                    remember_yearly=True))
        self.assertTrue(self.perms.covers("FI-D0001", "XMAS_EVE", "closed"))
        self.assertFalse(self.perms.covers("FI-D0001", "XMAS_EVE", "10:00-14:00"))
        self.assertIsNone(self.perms.lookup("FI-D0001", "NYE"))
        # next year: no question, apply + inform
        self.mock.reset()
        res = self.applier.apply(Decision("FI-D0001", [DecisionItem("2026-12-24", "closed", day_key="XMAS_EVE")],
                                          actor="standing_permission"))
        self.assertEqual(res.items[0].status, APPLIED)


class Undo(MockTestCase):
    def test_undo_restores_previous_value(self):
        res = self.applier.apply(Decision("FI-D0013", [DecisionItem("2026-12-24", "09:00-12:00")]))
        u = self.applier.undo(res.audit_id)
        self.assertEqual([i.status for i in u.items], [UNDONE])
        self.assertEqual(self.hol("FI-D0013")["2026-12-24"], "closed")
        again = self.applier.undo(res.audit_id)
        self.assertEqual(again.items[0].status, BLOCKED)          # can't undo twice

    def test_undo_removes_a_date_that_did_not_exist(self):
        res = self.applier.apply(Decision("FI-D0001", [DecisionItem("2026-12-24", "closed")]))
        self.applier.undo(res.audit_id)
        self.assertNotIn("2026-12-24", self.hol("FI-D0001"))

    def test_undo_leaves_later_changes_alone(self):
        res = self.applier.apply(Decision("FI-D0001", [DecisionItem("2026-12-24", "closed")]))
        self.client.update_entity("FI-D0001", {"hours": H.with_holiday_changes(
            self.client.get_entity("FI-D0001")["hours"], {"2026-12-24": "12:00-15:00"})})
        u = self.applier.undo(res.audit_id)
        self.assertEqual(u.items[0].status, CONFLICT)
        self.assertEqual(self.hol("FI-D0001")["2026-12-24"], "12:00-15:00")


class SandboxMirror(MockTestCase):
    def paired(self):
        return Applier(self.client, self.audit, self.perms, CONTACTS, today=TODAY,
                       sandbox=YextClient(self.mock.url, 'demo', name='sandbox'), mirror={'FI-D0001': 'FI-D0003'})

    def sandbox_hours(self, hours):
        self.client.update_entity('FI-D0003', {'hours': H.with_holiday_changes(
            self.client.get_entity('FI-D0003')['hours'], {'2026-12-24': hours})})

    def test_mirror_writes_the_same_dates_to_the_paired_business(self):
        # stand-in for the sandbox: a second client pointed at the same mock, pairing FI-D0001 -> FI-D0003
        applier = Applier(self.client, self.audit, self.perms, CONTACTS, today=TODAY,
                          sandbox=YextClient(self.mock.url, "demo", name="sandbox"), mirror={"FI-D0001": "FI-D0003"})
        res = applier.apply(Decision("FI-D0001", [DecisionItem("2026-12-24", "closed")]))
        self.assertTrue(res.mirror["ok"])
        self.assertEqual(self.hol("FI-D0003")["2026-12-24"], "closed")
        applier.undo(res.audit_id)
        self.assertNotIn("2026-12-24", self.hol("FI-D0003"))

    def test_undo_restores_sandbox_own_previous_exception(self):
        self.sandbox_hours('11:00-14:00')
        applier = self.paired()
        res = applier.apply(Decision('FI-D0001', [DecisionItem('2026-12-24', 'closed')], suggestion_id='different-before'))
        undo = applier.undo(res.audit_id)
        self.assertTrue(undo.mirror['ok'])
        self.assertEqual(self.hol('FI-D0003')['2026-12-24'], '11:00-14:00')
        self.assertNotIn('2026-12-24', self.hol('FI-D0001'))

    def test_undo_does_not_remove_preexisting_matching_sandbox_hours(self):
        self.sandbox_hours('closed')
        applier = self.paired()
        res = applier.apply(Decision('FI-D0001', [DecisionItem('2026-12-24', 'closed')]))
        self.assertFalse(res.mirror['written'])
        applier.undo(res.audit_id)
        self.assertEqual(self.hol('FI-D0003')['2026-12-24'], 'closed')

    def test_undo_preserves_later_sandbox_edit(self):
        applier = self.paired()
        res = applier.apply(Decision('FI-D0001', [DecisionItem('2026-12-24', 'closed')]))
        self.sandbox_hours('12:00-16:00')
        undo = applier.undo(res.audit_id)
        self.assertFalse(undo.mirror['ok'])
        self.assertIn('changed after', undo.mirror['errors']['2026-12-24'])
        self.assertEqual(self.hol('FI-D0003')['2026-12-24'], '12:00-16:00')

    def test_undo_includes_successful_mirror_retry(self):
        self.sandbox_hours('11:00-14:00')
        applier = self.paired()
        decision = Decision('FI-D0001', [DecisionItem('2026-12-24', 'closed')], suggestion_id='retry-mirror')
        with mock.patch.object(applier.sandbox, 'get_entity', side_effect=YextError(503, 'temporary')):
            res = applier.apply(decision)
        self.assertFalse(res.mirror['ok'])
        retry = applier.apply(decision)
        self.assertTrue(retry.mirror['ok'])
        self.assertTrue(applier.undo(res.audit_id).mirror['ok'])
        self.assertEqual(self.hol('FI-D0003')['2026-12-24'], '11:00-14:00')


class FakeClient:
    """Minimal in-memory Yext for failure paths the mock can't produce."""

    def __init__(self, drop_holidays=False, fail_put=False, touch_weekdays=False):
        self.name = "fake"
        self.hours = {d: {"openIntervals": [{"start": "09:00", "end": "17:00"}]} for d in H.DAYS}
        self.drop, self.fail, self.touch = drop_holidays, fail_put, touch_weekdays
        self.puts = []

    def get_entity(self, eid):
        return {"name": "Fake Café", "hours": json.loads(json.dumps(self.hours))}

    def update_entity(self, eid, fields):
        self.puts.append(fields)
        if self.fail:
            raise YextError(400, "hours invalid")
        h = json.loads(json.dumps(fields["hours"]))
        if self.drop and len(self.puts) == 1:            # NON_FATAL_ERROR: field silently rejected
            h.pop("holidayHours", None)
            self.hours = h
            return {"meta": {"errors": [{"type": "NON_FATAL_ERROR", "message": "holidayHours rejected"}]}}
        if self.touch and len(self.puts) == 1:
            h["monday"] = {"isClosed": True}
        self.hours = h
        return {"meta": {"uuid": "x", "errors": []}}


class FailurePaths(unittest.TestCase):
    def make(self, client):
        tmp = Path(tempfile.mkdtemp())
        return Applier(client, AuditLog(tmp / "a.jsonl"), StandingPermissions(tmp / "p.json"), {}, today=TODAY)

    def test_2xx_with_dropped_field_is_reported_as_failed(self):
        res = self.make(FakeClient(drop_holidays=True)).apply(Decision("X", [DecisionItem("2026-12-24", "closed")]))
        self.assertEqual(res.items[0].status, FAILED)
        self.assertIn("holidayHours rejected", res.items[0].reason)
        self.assertIsNone(res.audit_id)

    def test_write_error_is_failed_not_raised(self):
        res = self.make(FakeClient(fail_put=True)).apply(Decision("X", [DecisionItem("2026-12-24", "closed")]))
        self.assertEqual(res.items[0].status, FAILED)
        self.assertIn("didn't go through", C.status_message("failed", "X", "2026-12-24", "closed", "en"))

    def test_collateral_change_is_rolled_back(self):
        fake = FakeClient(touch_weekdays=True)
        res = self.make(fake).apply(Decision("X", [DecisionItem("2026-12-24", "closed")]))
        self.assertEqual(res.items[0].status, FAILED)
        self.assertEqual(H.item_to_str(fake.hours["monday"]), "09:00-17:00")   # restored


class ClientRetries(unittest.TestCase):
    def _http_error(self, code, headers=None):
        return urllib.error.HTTPError("u", code, "err", headers or {}, io.BytesIO(b'{"meta":{"errors":[]}}'))

    def test_retries_5xx_then_succeeds_and_uses_header_auth(self):
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b'{"response": {"id": "A"}}'
        sleeps = []
        c = YextClient("https://sbx-api.yextapis.com", "SECRET", sleep=sleeps.append)
        with mock.patch("urllib.request.urlopen", side_effect=[self._http_error(503), ok]) as uo:
            self.assertEqual(c.get_entity("A"), {"id": "A"})
        req = uo.call_args[0][0]
        self.assertNotIn("SECRET", req.full_url)
        self.assertEqual(req.get_header("Api-key"), "SECRET")
        self.assertIn("v=20261106", req.full_url)
        self.assertEqual(sleeps, [1])

    def test_429_waits_for_retry_header_and_400_does_not_retry(self):
        sleeps = []
        c = YextClient("https://x", "k", sleep=sleeps.append, max_retries=1)
        with mock.patch("urllib.request.urlopen",
                        side_effect=[self._http_error(429, {"Rate-Limit-Retry-At": "7"}), self._http_error(400)]):
            with self.assertRaises(YextError) as e:
                c.get_entity("A")
        self.assertEqual(e.exception.status, 400)
        self.assertEqual(sleeps, [7.0])


class Messages(unittest.TestCase):
    def test_all_languages_render(self):
        for lang in ("fi", "sv", "nb", "en", "de"):
            msg = C.applied_message("Kafé Linnea", [("2026-12-24", "closed"), ("2026-12-13", "07:00-15:30")],
                                    "SE-D0008", lang)
            self.assertIn("Kafé Linnea", msg)
            self.assertTrue(msg.splitlines()[1].startswith("•"))

    def test_hours_helpers(self):
        self.assertEqual(H.normalise(" 9:00 – 14:30 "), "09:00-14:30")
        self.assertEqual(H.pretty("10:00-14:30,17:00-22:00"), "10–14.30, 17–22")
        self.assertEqual(H.validate("22:00-02:00"), [])
        self.assertEqual(H.validate("00:00-23:59"), [])
        self.assertTrue(H.validate("25:00-26:00"))
        self.assertEqual(C.eta(120, "en"), "~2 min")
        self.assertEqual(C.eta(86400, "en"), "~1 day")


if __name__ == "__main__":
    unittest.main()
