"""Offline contract tests: explicit doubles, no external API calls or credentials."""
import copy
import os
import tempfile
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from step5_apply.api import Step5
from step5_apply.yext import MappedYextClient, YextError


class SandboxDouble:
    base = "https://sbx-api.yextapis.com"
    name = "sandbox"

    def __init__(self):
        self.timeout, self.max_retries = 30, 3
        self.entity = {"name": "Sandbox test entity", "hours": {"monday": {"isClosed": True}}}
        self.calls = []

    def get_entity(self, eid):
        self.calls.append(("GET", eid))
        return copy.deepcopy(self.entity)

    def update_entity(self, eid, fields):
        self.calls.append(("PUT", eid))
        self.entity.update(copy.deepcopy(fields))
        return {"meta": {"uuid": "offline-test"}, "response": {}}


class SandboxMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"YEXT_API_KEY": "offline-test-key",
                                     "YEXT_SANDBOX_MIRROR": "FI-D0001:Sandbox test entity"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        dotenv = patch("step5_apply.config.load_dotenv")
        dotenv.start()
        self.addCleanup(dotenv.stop)
        self.mock_start = patch("step5_apply.api.start_mock", side_effect=AssertionError("No mock fallback"))
        self.mock_start.start()
        self.addCleanup(self.mock_start.stop)

    def test_missing_key_and_production_url_fail_closed(self):
        with patch.dict(os.environ, {"YEXT_API_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "no mock fallback"):
                Step5.from_env(mode="sandbox", runtime_dir=self.tmp.name)
        with patch.dict(os.environ, {"YEXT_BASE_URL": "https://api.yextapis.com"}):
            with self.assertRaisesRegex(ValueError, "sandbox mode requires"):
                Step5.from_env(mode="sandbox", runtime_dir=self.tmp.name)

    def test_mapping_rejects_ambiguous_and_unknown_entities(self):
        client = SandboxDouble()
        with self.assertRaisesRegex(ValueError, "one-to-one"):
            MappedYextClient(client, {"A": "same", "B": "same"})
        mapped = MappedYextClient(client, {"A": "sandbox-A"})
        with self.assertRaisesRegex(YextError, "No sandbox entity mapping"):
            mapped.update_entity("B", {"hours": {}})
        self.assertEqual(client.calls, [])





    def test_approval_conflict_undo_use_real_target_contract(self):
        client = SandboxDouble()
        with patch("step5_apply.api.YextClient", return_value=client):
            s5 = Step5.from_env(mode="sandbox", runtime_dir=self.tmp.name, today=date(2026, 11, 6))
        self.assertIsNone(s5.mock)
        seen = s5.snapshot("FI-D0001", ["2026-12-24"])
        decision = {"entity_id": "FI-D0001", "suggestion_id": "test-approval", "items": [
            {"date": "2026-12-24", "hours": "closed", "decision": "ACCEPTED", "seen_before": seen["2026-12-24"]}]}
        result = s5.apply(decision)
        self.assertEqual(result["items"][0]["status"], "APPLIED")
        self.assertTrue(all(eid == "Sandbox test entity" for _, eid in client.calls))
        repeat = s5.apply(decision)
        self.assertTrue(repeat["items"][0]["duplicate"])
        self.assertIsNone(repeat["message"])
        decision["items"][0]["hours"] = "10:00-14:00"
        conflict = s5.apply(decision)
        self.assertEqual(conflict["items"][0]["status"], "CONFLICT")
        undone = s5.undo(entity_id="FI-D0001")
        self.assertEqual(undone["items"][0]["status"], "UNDONE")
        self.assertEqual(s5.snapshot("FI-D0001", ["2026-12-24"]), seen)


if __name__ == "__main__":
    unittest.main()
