"""Regression tests for the Yext failure modes found in review: transport errors, unreadable responses, rate limits,
past holiday dates, sandbox mirror retries, partial undo, and audit-log concurrency.

    python -m unittest discover -s step5_apply/tests -t . -v      # from the repository root
"""
from __future__ import annotations

import http.client
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import date
from pathlib import Path
from unittest import mock

from step5_apply import hours as H
from step5_apply.apply import ALREADY_SET, APPLIED, BLOCKED, FAILED, UNDONE, Applier, Decision, DecisionItem
from step5_apply.mock_runtime import start_mock
from step5_apply.store import AuditLog, StandingPermissions
from step5_apply.yext import YextClient, YextError

TODAY = date(2026, 11, 6)


class Fake:
    """In-memory Yext entity with switchable misbehaviour, counted per PUT (1-based)."""

    def __init__(self, holidays: dict[str, str] | None = None, *, fail_puts: dict[int, YextError] | None = None,
                 lands_then_raises: set[int] = frozenset(), prune_before: str | None = None,
                 reject_before: str | None = None, ignore_date: dict[int, str] | None = None,
                 touch_weekday_on: set[int] = frozenset()):
        self.name = "fake"
        self.hours = {d: {"openIntervals": [{"start": "09:00", "end": "17:00"}]} for d in H.DAYS}
        if holidays:
            self.hours = H.with_holiday_changes(self.hours, holidays)
        self.fail_puts, self.lands_then_raises = fail_puts or {}, lands_then_raises
        self.prune_before, self.reject_before, self.ignore_date = prune_before, reject_before, ignore_date or {}
        self.touch_weekday_on = touch_weekday_on
        self.puts: list[dict] = []

    def get_entity(self, eid):
        h = json.loads(json.dumps(self.hours))
        if not h.get("holidayHours"):
            h.pop("holidayHours", None)                 # like Yext: no key at all when there are none
        return {"name": "Fake Café", "hours": h}

    def update_entity(self, eid, fields):
        self.puts.append(fields)
        n = len(self.puts)
        if n in self.fail_puts:
            raise self.fail_puts[n]
        h = json.loads(json.dumps(fields["hours"]))
        if "holidayHours" not in h:                     # like Yext: an absent key keeps the existing dates
            h["holidayHours"] = json.loads(json.dumps(self.hours.get("holidayHours", [])))
        if n in self.touch_weekday_on:
            h["monday"] = {"isClosed": True}
        hol = h.get("holidayHours", [])
        if self.reject_before and any(x["date"] < self.reject_before for x in hol):
            raise YextError(400, "holidayHours: date is in the past")
        if self.prune_before:
            h["holidayHours"] = [x for x in hol if x["date"] >= self.prune_before]
        if n in self.ignore_date:                       # Yext silently keeps the old value for one date
            d = self.ignore_date[n]
            old = [x for x in self.hours.get("holidayHours", []) if x["date"] == d]
            h["holidayHours"] = sorted([x for x in h.get("holidayHours", []) if x["date"] != d] + old,
                                       key=lambda x: x["date"])
        self.hours = h
        if n in self.lands_then_raises:
            raise YextError(None, "PUT entities/X: ConnectionResetError: reset by peer")
        return {"meta": {"uuid": f"put-{n}", "errors": []}}


def make(primary, sandbox=None, mirror=None):
    tmp = Path(tempfile.mkdtemp())
    return Applier(primary, AuditLog(tmp / "a.jsonl"), StandingPermissions(tmp / "p.json"), {}, today=TODAY,
                   sandbox=sandbox, mirror=mirror)


class Transport(unittest.TestCase):
    def ok(self, body: bytes):
        r = mock.MagicMock()
        r.__enter__.return_value.read.return_value = body
        return r

    def test_connection_reset_is_retried_then_becomes_yext_error(self):
        sleeps = []
        c = YextClient("https://x", "k", sleep=sleeps.append, max_retries=2)
        errs = [ConnectionResetError("reset"), http.client.RemoteDisconnected("gone"), http.client.IncompleteRead(b"")]
        with mock.patch("urllib.request.urlopen", side_effect=errs):
            with self.assertRaises(YextError) as e:
                c.get_entity("A")
        self.assertIsNone(e.exception.status)          # outcome unknown
        self.assertEqual(sleeps, [1, 2])

    def test_connection_reset_then_success(self):
        c = YextClient("https://x", "k", sleep=lambda s: None)
        with mock.patch("urllib.request.urlopen", side_effect=[ConnectionResetError(), self.ok(b'{"response": {}}')]):
            self.assertEqual(c.get_entity("A"), {})

    def test_non_json_2xx_is_a_yext_error_not_value_error(self):
        c = YextClient("https://x", "k", sleep=lambda s: None)
        with mock.patch("urllib.request.urlopen", return_value=self.ok(b"<html>gateway</html>")):
            with self.assertRaises(YextError) as e:
                c.get_entity("A")
        self.assertIsNone(e.exception.status)
        self.assertNotIsInstance(e.exception, ValueError)

    def test_429_with_distant_reset_fails_fast(self):
        sleeps = []
        c = YextClient("https://x", "k", sleep=sleeps.append)
        err = urllib.error.HTTPError("u", 429, "slow down", {"Rate-Limit-Reset": str(time.time() + 1800)},
                                     io.BytesIO(b"{}"))
        with mock.patch("urllib.request.urlopen", side_effect=[err]):
            with self.assertRaises(YextError) as e:
                c.get_entity("A")
        self.assertEqual(e.exception.status, 429)
        self.assertEqual(sleeps, [])

    def test_entity_id_slash_is_escaped(self):
        c = YextClient("https://x", "k")
        with mock.patch("urllib.request.urlopen", return_value=self.ok(b'{"response": {}}')) as uo:
            c.get_entity("Bar 24/7")
        self.assertIn("/entities/Bar%2024%2F7?", uo.call_args[0][0].full_url)


class ListingsAgainstMock(unittest.TestCase):
    def test_listings_filter_uses_entity_ids(self):
        m = start_mock()
        try:
            c = YextClient(m.url, "demo")
            rows = c.listings(["FI-D0001"])
            self.assertTrue(rows)
            self.assertEqual({r["entityId"] for r in rows}, {"FI-D0001"})
        finally:
            m.stop()


class UnknownOutcomeWrites(unittest.TestCase):
    def test_put_that_landed_despite_network_error_is_applied_and_audited(self):
        fake = Fake(lands_then_raises={1})
        a = make(fake)
        res = a.apply(Decision("X", [DecisionItem("2026-12-24", "closed")]))
        self.assertEqual(res.items[0].status, APPLIED)
        self.assertIsNotNone(res.audit_id)               # so it can still be undone
        self.assertEqual(a.undo(res.audit_id).items[0].status, UNDONE)

    def test_put_that_did_not_land_is_failed(self):
        a = make(Fake(fail_puts={1: YextError(None, "timeout")}))
        res = a.apply(Decision("X", [DecisionItem("2026-12-24", "closed")]))
        self.assertEqual(res.items[0].status, FAILED)
        self.assertIn("timeout", res.items[0].reason)
        self.assertIsNone(res.audit_id)

    def test_service_survives_raw_socket_error_from_client(self):
        # even a non-Yext exception path ends in YextError now, so apply() never raises for transport problems
        c = YextClient("https://x", "k", sleep=lambda s: None, max_retries=0)
        a = make(c)
        with mock.patch("urllib.request.urlopen", side_effect=ConnectionResetError("reset")):
            res = a.apply(Decision("X", [DecisionItem("2026-12-24", "closed")]))
        self.assertEqual(res.items[0].status, FAILED)


class PastHolidayDates(unittest.TestCase):
    def test_yext_pruning_past_dates_is_not_a_collateral_change(self):
        fake = Fake({"2025-12-24": "closed", "2026-12-31": "10:00-14:00"}, prune_before=TODAY.isoformat())
        res = make(fake).apply(Decision("X", [DecisionItem("2026-12-24", "closed")]))
        self.assertEqual(res.items[0].status, APPLIED)
        self.assertEqual(len(fake.puts), 1)              # no restore PUT
        self.assertEqual(H.holiday_map(fake.hours)["2026-12-31"], "10:00-14:00")

    def test_yext_rejecting_past_dates_retries_without_them(self):
        fake = Fake({"2025-12-24": "closed"}, reject_before=TODAY.isoformat())
        res = make(fake).apply(Decision("X", [DecisionItem("2026-12-24", "closed")]))
        self.assertEqual(res.items[0].status, APPLIED)
        self.assertEqual(len(fake.puts), 2)
        self.assertNotIn("2025-12-24", H.holiday_map(fake.hours))

    def test_other_4xx_is_not_retried(self):
        fake = Fake(fail_puts={1: YextError(400, "hours invalid")})
        res = make(fake).apply(Decision("X", [DecisionItem("2026-12-24", "closed")]))
        self.assertEqual(res.items[0].status, FAILED)
        self.assertEqual(len(fake.puts), 1)


class CollateralRestore(unittest.TestCase):
    def test_restore_removes_the_new_date_when_entity_had_no_holidays(self):
        fake = Fake(touch_weekday_on={1})               # the write also flips Monday: must be rolled back
        res = make(fake).apply(Decision("X", [DecisionItem("2026-12-24", "closed")]))
        self.assertEqual(res.items[0].status, FAILED)
        self.assertIn("original hours restored", res.items[0].reason)
        self.assertEqual(H.holiday_map(fake.hours), {})  # not left behind by a PUT without holidayHours
        self.assertEqual(H.item_to_str(fake.hours["monday"]), "09:00-17:00")


class MirrorRetry(unittest.TestCase):
    def test_failed_mirror_is_retried_on_repeat(self):
        primary, sandbox = Fake(), Fake(fail_puts={1: YextError(503, "unavailable"), 2: YextError(503, "x"),
                                                   3: YextError(503, "x"), 4: YextError(503, "x")})
        a = make(primary, sandbox, {"X": "S"})
        d = lambda: Decision("X", [DecisionItem("2026-12-24", "closed")], suggestion_id="msg-1")
        first = a.apply(d())
        self.assertEqual(first.items[0].status, APPLIED)
        self.assertFalse(first.mirror["ok"])
        sandbox.fail_puts = {}
        again = a.apply(d())
        self.assertEqual(again.items[0].status, ALREADY_SET)
        self.assertTrue(again.mirror["ok"] and again.mirror["written"])
        self.assertEqual(H.holiday_map(sandbox.hours)["2026-12-24"], "closed")
        self.assertEqual(again.audit_id, first.audit_id)
        self.assertEqual([r["type"] for r in a.audit.all()], ["APPLY", "MIRROR"])
        # third time: nothing left to do anywhere
        n = len(sandbox.puts)
        third = a.apply(d())
        self.assertFalse(third.mirror["written"])
        self.assertEqual(len(sandbox.puts), n)
        # undo also follows on the sandbox, even though the original APPLY recorded a failed mirror
        a.undo(first.audit_id)
        self.assertNotIn("2026-12-24", H.holiday_map(sandbox.hours))


class PartialUndo(unittest.TestCase):
    def test_failed_date_of_a_partial_undo_can_be_retried(self):
        fake = Fake(ignore_date={2: "2026-12-31"})       # the undo PUT (2nd) silently keeps 31 Dec
        a = make(fake)
        res = a.apply(Decision("X", [DecisionItem("2026-12-24", "closed"), DecisionItem("2026-12-31", "10:00-14:00")]))
        self.assertEqual([i.status for i in res.items], [APPLIED, APPLIED])
        u1 = a.undo(res.audit_id)
        self.assertEqual([i.status for i in u1.items], [UNDONE, FAILED])
        self.assertEqual(a.latest_apply("X"), res.audit_id)          # not fully undone yet
        u2 = a.undo(res.audit_id)
        self.assertEqual([i.status for i in u2.items], [BLOCKED, UNDONE])
        self.assertEqual(H.holiday_map(fake.hours), {})
        self.assertIsNone(a.latest_apply("X"))
        self.assertEqual([i.status for i in a.undo(res.audit_id).items], [BLOCKED, BLOCKED])


class AuditConcurrency(unittest.TestCase):
    def test_reads_never_see_a_half_written_line(self):
        log = AuditLog(Path(tempfile.mkdtemp()) / "a.jsonl")
        big = {"type": "APPLY", "entity_id": "X", "items": [{"date": "2026-12-24", "pad": "x" * 200_000}]}
        errors = []

        def writer():
            for _ in range(30):
                log.append(dict(big))

        def reader():
            for _ in range(60):
                try:
                    log.all()
                except ValueError as e:                  # JSONDecodeError
                    errors.append(e)

        ts = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(log.all()), 30)


if __name__ == "__main__":
    unittest.main()
