"""Integration boundary checks between the current demo and real messaging."""
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import messaging
from messaging_bridge import prepare, parse


class BridgeTests(unittest.TestCase):
    def test_real_proposal_uses_current_destination_and_future_model_rows(self):
        days = [date(2020, 12, 24), date(2099, 12, 24), date(2099, 12, 25)]
        app = SimpleNamespace(
            BL=SimpleNamespace(ents={"A": {}}), TARGETS={"A": days},
            suggestion=lambda eid, day: {"day": day, "action": "SUGGEST", "contact_eligible": True,
                "hours": "closed", "last_hours": None, "weekday": "Thursday", "dm": str(day),
                "day_name": "Christmas", "name": "Cafe"},
            period_of=lambda item: ("christmas", "Christmas"),
            intro=lambda *args: "Christmas is coming.", nice=lambda value: value)
        source = SimpleNamespace(policy=lambda eid: {"opted_out": False},
                                 snapshot=Mock(return_value={"2099-12-24": "closed", "2099-12-25": None}))
        result = prepare(app, "A", "christmas", source)
        source.snapshot.assert_called_once_with("A", ["2099-12-24", "2099-12-25"])
        self.assertEqual([x["date"] for x in result["items"]], ["2099-12-25"])
        self.assertNotIn("last year", result["detail"])
        source.policy = lambda eid: {"opted_out": True}
        with self.assertRaises(messaging.MessagingError):
            prepare(app, "A", "christmas", source)

    def test_configured_ai_failure_does_not_apply_partial_rule_interpretation(self):
        rules = Mock()
        app = SimpleNamespace(hours_ai=SimpleNamespace(configured=lambda: True, read_reply=lambda *a: None),
                              parse_hours_reply=rules)
        found, problems = parse(app, "24 Dec 10-14", [{"date": "2099-12-24"}])
        self.assertEqual(found, {})
        self.assertTrue(problems)
        rules.assert_not_called()

    def test_no_credentials_supports_exact_edit_format(self):
        app = SimpleNamespace(hours_ai=SimpleNamespace(configured=lambda: False),
                              parse_hours_reply=lambda *a: ({date(2099, 12, 24): "10:00-14:00"}, []))
        self.assertEqual(parse(app, "24 Dec 10-14", [{"date": "2099-12-24"}]),
                         ({"2099-12-24": "10:00-14:00"}, []))


if __name__ == "__main__":
    unittest.main()
