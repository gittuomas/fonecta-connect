"""Offline contract tests: isolated test doubles never send messages or access Yext."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
import messaging
from twilio.request_validator import RequestValidator


class FakeTransport:
    account = "AC" + "a" * 32
    sender = "whatsapp:+358401234567"
    public = "https://example.test"

    def __init__(self):
        self.validator = RequestValidator("test-token")
        self.sent = []
        self.fail = False

    def validate(self, path, form, signature):
        return self.validator.validate(self.public + path, form, signature)

    def send(self, recipient, operation, **kwargs):
        if self.fail:
            raise TimeoutError()
        self.sent.append((recipient, operation, kwargs))
        return "SM" + format(len(self.sent), "032x"), "queued"


class FakeStep5:
    def __init__(self):
        self.calls = []
        self.undos = []
        self.opted_out = False

    def policy(self, eid):
        return {"opted_out": self.opted_out}

    def snapshot(self, eid, dates):
        return {d: "09:00-17:00" for d in dates}

    def apply(self, body):
        self.calls.append(body)
        return {"audit_id": "audit-1", "items": [{"date": x["date"], "hours": x["hours"],
                "status": "REJECTED" if x["decision"] == "REJECTED" else "APPLIED"} for x in body["items"]]}

    def undo(self, **kwargs):
        self.undos.append(kwargs)
        return {"items": [{"date": "2099-12-24", "hours": "09:00-17:00", "status": "UNDONE"}]}


class MessagingTests(unittest.TestCase):
    phone = "whatsapp:+358409876543"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.transport, self.step5 = FakeTransport(), FakeStep5()
        self.contacts = {"FI-D0001": {"address": self.phone, "opted_in": True}}
        self.payload = {"name": "Cafe", "detail": "We suggest: closed", "question": "Update?",
                        "items": [{"date": "2099-12-24", "hours": "closed"}]}
        self.service = self.make_service()
        self.seq = 100

    def make_service(self):
        return messaging.Messaging(self.tmp.name, self.transport, self.step5,
                                   lambda eid, pid: dict(self.payload),
                                   lambda text, items: ({items[0]["date"]: "10:00-14:00"}, []) if text == "24 Dec 10-14" else ({}, [text]),
                                   self.contacts)

    def propose(self):
        return self.service.send_proposal("FI-D0001", "christmas")["reference"]

    def webhook(self, text, *, signature=None, sender=None, sid=None, button=False):
        self.seq += 1
        form = {"AccountSid": self.transport.account, "To": self.transport.sender,
                "From": sender or self.phone, "MessageSid": sid or "SM" + format(self.seq, "032x"),
                "Body": text}
        if button:
            form["ButtonPayload"] = text
            form["Body"] = "Yes, update all"
        path = "/messaging/inbound"
        signature = signature if signature is not None else self.transport.validator.compute_signature(self.transport.public + path, form)
        self.service.webhook(path, urlencode(form).encode(), signature)
        return form

    def test_unconfigured_does_not_construct_transport(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(messaging.MessagingError, "unconfigured"):
                messaging.TwilioTransport()

    def test_sdk_transport_builds_real_provider_requests(self):
        env = {key: "test" for key in messaging.REQUIRED}
        env.update(TWILIO_ACCOUNT_SID=self.transport.account, TWILIO_WHATSAPP_FROM=self.transport.sender,
                   TWILIO_CONTENT_SID="HX" + "b" * 32, CONNECT_PUBLIC_URL="https://example.test")
        with patch.dict(os.environ, env, clear=True):
            transport = messaging.TwilioTransport()
        with patch.object(transport.client.messages, "create", return_value=SimpleNamespace(sid="SM" + "a" * 32, status="queued")) as create:
            transport.send(self.phone, "abc", variables={"1": "Cafe"})
            self.assertEqual(create.call_args.kwargs["content_sid"], "HX" + "b" * 32)
            self.assertEqual(create.call_args.kwargs["status_callback"], "https://example.test/messaging/status?operation=abc")
            self.assertNotIn("body", create.call_args.kwargs)
            transport.send(self.phone, "def", body="Verified in sandbox")
            self.assertEqual(create.call_args.kwargs["body"], "Verified in sandbox")
            self.assertNotIn("content_sid", create.call_args.kwargs)

    def test_send_uses_template_and_persists_snapshot(self):
        ref = self.propose()
        variables = self.transport.sent[0][2]["variables"]
        self.assertEqual(variables["4"], ref)
        with self.service.db() as db:
            saved = json.loads(db.execute("SELECT payload FROM proposals").fetchone()[0])
        self.assertEqual(saved["seen"], {"2099-12-24": "09:00-17:00"})
        with self.assertRaises(messaging.MessagingError):
            self.propose()
        self.assertEqual(len(self.transport.sent), 1)

    def test_template_variables_flatten_whitespace_and_enforce_full_body_limit(self):
        self.payload['detail'] = 'We suggest:\n24 Dec\tclosed.    Keep existing dates.'
        self.propose()
        variables = self.transport.sent[0][2]['variables']
        self.assertEqual(variables['2'], 'We suggest: 24 Dec closed. Keep existing dates.')
        payload = dict(self.payload, detail='x' * 1024)
        with self.assertRaisesRegex(messaging.MessagingError, 'body limit'):
            messaging.template_variables(payload, 'deadbeef')

    def test_signature_or_wrong_account_cannot_queue(self):
        with self.assertRaises(PermissionError):
            self.webhook("YES deadbeef", signature="forged")
        self.assertEqual(self.service.status()["incoming"], [])

    def test_unknown_sender_is_ignored(self):
        self.webhook("HELP", sender="whatsapp:+358400000001")
        self.assertFalse(self.service.process_one())
        self.assertEqual(self.transport.sent, [])

    def test_approve_is_durable_and_duplicate_webhook_is_once(self):
        ref = self.propose()
        sid = "SM" + "f" * 32
        self.webhook("YES " + ref, sid=sid, button=True)
        self.webhook("YES " + ref, sid=sid, button=True)
        self.assertEqual(self.step5.calls, [])  # ingress never blocks on external Apply.
        self.service = self.make_service()  # restart before processing
        self.assertTrue(self.service.process_one())
        self.assertFalse(self.service.process_one())
        self.assertEqual(len(self.step5.calls), 1)
        self.assertEqual(self.step5.calls[0]["items"][0]["seen_before"], "09:00-17:00")
        self.assertIn("Yext sandbox", self.transport.sent[-1][2]["body"])
        self.webhook("YES " + ref)
        self.service.process_one()
        self.assertEqual(len(self.step5.calls), 1)

    def test_decline_never_approves(self):
        ref = self.propose()
        self.webhook("NO " + ref)
        self.service.process_one()
        self.assertEqual(self.step5.calls[0]["items"][0]["decision"], "REJECTED")
        self.assertEqual(self.transport.sent[-1][2]["body"], "OK, we won't change anything.")

    def test_edit_rejects_partial_parse_then_applies_exact_hours(self):
        ref = self.propose()
        self.webhook("EDIT " + ref + " nonsense")
        self.service.process_one()
        self.assertEqual(self.step5.calls, [])
        self.webhook("EDIT " + ref + " 24 Dec 10-14")
        self.service.process_one()
        self.assertEqual(self.step5.calls, [])
        self.assertIn('CONFIRM ' + ref, self.transport.sent[-1][2]['body'])
        self.service = self.make_service()  # Draft survives a worker restart.
        self.webhook('CONFIRM ' + ref)
        self.service.process_one()
        self.assertEqual(self.step5.calls[0]["items"][0]["hours"], "10:00-14:00")

    def test_matching_saved_hours_never_send_outreach(self):
        with patch.object(self.step5, 'snapshot', return_value={'2099-12-24': 'closed'}):
            with self.assertRaisesRegex(messaging.MessagingError, 'already match'):
                self.propose()
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(self.service.status()['proposals'], [])

    def test_cancel_or_invalid_edit_discards_pending_confirmation(self):
        ref = self.propose()
        for action in ('CANCEL ' + ref, 'EDIT ' + ref + ' nonsense'):
            self.webhook('EDIT ' + ref + ' 24 Dec 10-14'); self.service.process_one()
            self.webhook(action); self.service.process_one()
            self.webhook('CONFIRM ' + ref); self.service.process_one()
        self.assertEqual(self.step5.calls, [])

    def test_other_hours_accepts_plain_followup_and_confirmation(self):
        ref = self.propose()
        self.webhook('OTHER ' + ref); self.service.process_one()
        self.webhook('24 Dec 10-14'); self.service.process_one()
        self.assertEqual(self.step5.calls, [])
        self.assertIn('Please check', self.transport.sent[-1][2]['body'])
        self.webhook('CONFIRM'); self.service.process_one()
        self.assertEqual(self.step5.calls[0]['items'][0]['decision'], 'EDITED')
        self.assertEqual(self.step5.calls[0]['items'][0]['hours'], '10:00-14:00')

    def test_undo_is_scoped_to_proposal_audit(self):
        ref = self.propose()
        self.webhook("YES " + ref)
        self.service.process_one()
        self.webhook("UNDO " + ref)
        self.service.process_one()
        self.assertEqual(self.step5.undos, [{"audit_id": "audit-1", "language": "en"}])

    def test_reference_and_sender_are_required(self):
        self.propose()
        self.webhook("YES")
        self.service.process_one()
        self.webhook("YES deadbeef")
        self.service.process_one()
        self.assertEqual(self.step5.calls, [])

    def test_stop_blocks_send_and_queued_decision_until_start(self):
        ref = self.propose()
        self.webhook("YES " + ref)
        self.webhook("STOP")
        self.service.process_one()
        self.service.process_one()
        self.assertEqual(self.step5.calls, [])
        self.assertTrue(self.make_service().stopped(self.phone))
        with self.assertRaises(messaging.MessagingError):
            self.service.send_proposal("FI-D0001", "new-year")
        self.webhook("START")
        self.service.process_one()
        self.assertFalse(self.service.stopped(self.phone))

    def test_crm_opt_out_remains_in_force(self):
        self.step5.opted_out = True
        with self.assertRaises(messaging.MessagingError):
            self.propose()
        self.assertEqual(self.transport.sent, [])

    def test_timeout_is_not_retried(self):
        self.transport.fail = True
        with self.assertRaises(messaging.MessagingError):
            self.propose()
        self.assertEqual(self.service.status()["outgoing"][0]["state"], "needs_review")
        with self.assertRaises(messaging.MessagingError):
            self.propose()

    def test_expired_proposal_never_applies(self):
        ref = self.propose()
        with self.service.db() as db:
            db.execute("UPDATE proposals SET created=0")
        self.webhook("YES " + ref)
        self.service.process_one()
        self.assertEqual(self.step5.calls, [])

    def test_status_callbacks_are_signed_and_do_not_regress(self):
        self.propose()
        operation = self.service.status()["outgoing"][0]["id"]
        path = "/messaging/status?operation=" + operation
        for status in ("delivered", "sent", "read"):
            form = {"AccountSid": self.transport.account, "MessageSid": "SM" + format(1, "032x"), "MessageStatus": status}
            signature = self.transport.validator.compute_signature(self.transport.public + path, form)
            self.service.webhook(path, urlencode(form).encode(), signature)
        self.assertEqual(self.service.status()["outgoing"][0]["state"], "read")

    def test_failing_apply_is_held_for_review_not_replayed(self):
        ref = self.propose()
        self.webhook("YES " + ref)
        with patch.object(self.step5, "apply", side_effect=TimeoutError()):
            self.service.process_one()
        self.assertEqual(self.service.status()["incoming"][0]["state"], "needs_review")
        self.assertFalse(self.service.process_one())
        self.webhook("YES " + ref)
        self.service.process_one()
        self.assertEqual(self.step5.calls, [])

    def test_conflict_confirmation_never_claims_success(self):
        text = self.service._confirmation({"items": [{"date": "2099-12-24", "status": "CONFLICT"}]})
        self.assertIn("Not saved", text)
        self.assertNotIn("Verified in", text)

    def test_opt_in_and_unique_recipient_are_required(self):
        self.contacts["FI-D0001"]["opted_in"] = False
        with self.assertRaises(messaging.MessagingError):
            self.make_service()
        self.contacts["FI-D0001"]["opted_in"] = True
        self.contacts["FI-D0002"] = self.contacts["FI-D0001"].copy()
        with self.assertRaises(messaging.MessagingError):
            self.make_service()


if __name__ == "__main__":
    unittest.main()
