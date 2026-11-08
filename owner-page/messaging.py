"""Real WhatsApp transport. No mock transport or implicit recipient fallback.

The HTTP webhook only authenticates and durably queues events. A single worker
per runtime processes owner decisions, then sends actual Twilio API messages.
Ambiguous external failures are held for operator review, never blindly retried.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PHONE = re.compile(r"^whatsapp:\+[1-9][0-9]{7,14}$")
SID = re.compile(r"^[A-Z]{2}[a-fA-F0-9]{32}$")
REQUIRED = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM",
            "TWILIO_CONTENT_SID", "CONNECT_PUBLIC_URL", "CONNECT_MESSAGING_CONTACTS",
            "YEXT_API_KEY", "YEXT_SANDBOX_MIRROR")


class MessagingError(ValueError):
    pass


def configuration():
    """Return configuration health without revealing credentials or recipients."""
    missing = [key for key in REQUIRED if not os.environ.get(key)]
    return {"configured": not missing, "missing": missing,
            "provider": "Twilio WhatsApp", "apply_target": "Yext sandbox"}


def template_variables(payload, ref):
    # WhatsApp disallows newlines/tabs and repeated spaces inside substitutions.
    values = {str(i): re.sub(r"\s+", " ", str(value)).strip() for i, value in
              enumerate((payload["name"], payload["detail"], payload["question"], ref), 1)}
    template = json.loads(Path(__file__).with_name("whatsapp-template.json").read_text(encoding="utf-8"))
    body = template["types"]["twilio/quick-reply"]["body"]
    rendered = re.sub(r"\{\{([1-4])\}\}", lambda m: values[m.group(1)], body)
    if len(rendered) > 1024:
        raise MessagingError("Proposal exceeds WhatsApp template body limit; split/review before sending")
    return values


class TwilioTransport:
    def __init__(self):
        cfg = configuration()
        if not cfg["configured"]:
            raise MessagingError("Real messaging is unconfigured: " + ", ".join(cfg["missing"]))
        from twilio.rest import Client
        from twilio.http.http_client import TwilioHttpClient
        from twilio.request_validator import RequestValidator
        self.account = os.environ["TWILIO_ACCOUNT_SID"]
        self.sender = os.environ["TWILIO_WHATSAPP_FROM"]
        self.content = os.environ["TWILIO_CONTENT_SID"]
        self.public = os.environ["CONNECT_PUBLIC_URL"].rstrip("/")
        parsed = urlsplit(self.public)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise MessagingError("CONNECT_PUBLIC_URL must be the public HTTPS origin, with no path/query/user info")
        if not PHONE.fullmatch(self.sender):
            raise MessagingError("TWILIO_WHATSAPP_FROM must be whatsapp:+<E.164 number>")
        if not SID.fullmatch(self.account) or not self.account.startswith("AC"):
            raise MessagingError("Invalid TWILIO_ACCOUNT_SID")
        if not SID.fullmatch(self.content) or not self.content.startswith("HX"):
            raise MessagingError("Invalid TWILIO_CONTENT_SID")
        token = os.environ["TWILIO_AUTH_TOKEN"]
        self.validator = RequestValidator(token)
        self.client = Client(self.account, token, http_client=TwilioHttpClient(timeout=10, max_retries=0))

    def validate(self, path, form, signature):
        # Do not trust Host/X-Forwarded-* supplied by the caller.
        return self.validator.validate(self.public + path, form, signature)

    def send(self, recipient, operation, *, variables=None, body=None):
        args = {"from_": self.sender, "to": recipient,
                "status_callback": self.public + "/messaging/status?operation=" + operation}
        if variables is not None:
            args.update(content_sid=self.content, content_variables=json.dumps(variables, ensure_ascii=False))
        else:
            args["body"] = body
        result = self.client.messages.create(**args)
        return result.sid, result.status


def real_step5(runtime):
    from step5_apply.api import Step5
    return Step5.from_env(mode="sandbox", runtime_dir=runtime)


class Messaging:
    def __init__(self, runtime, transport, step5, prepare, parse_hours, contacts):
        self.runtime = Path(runtime)
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.db_path = self.runtime / "messaging.sqlite3"
        self.transport, self.step5 = transport, step5
        self.prepare, self.parse_hours, self.contacts = prepare, parse_hours, contacts
        self.stop_event = threading.Event()
        phones = []
        for eid, contact in contacts.items():
            phone = contact.get("address", "")
            if not PHONE.fullmatch(phone) or contact.get("opted_in") is not True:
                raise MessagingError("Every messaging contact needs a valid WhatsApp address and explicit opted_in: true")
            phones.append(phone)
        if len(phones) != len(set(phones)):
            raise MessagingError("Use one business per WhatsApp recipient to prevent ambiguous ownership")
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS proposals (
                  ref TEXT PRIMARY KEY, eid TEXT, pid TEXT, recipient TEXT,
                  payload TEXT, created REAL, state TEXT, audit_id TEXT,
                  UNIQUE(eid,pid));
                CREATE TABLE IF NOT EXISTS outgoing (
                  id TEXT PRIMARY KEY, ref TEXT, recipient TEXT, sid TEXT,
                  state TEXT, error TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS incoming (
                  sid TEXT PRIMARY KEY, payload TEXT, state TEXT, created REAL, error TEXT);
                CREATE TABLE IF NOT EXISTS preferences (recipient TEXT PRIMARY KEY, stopped INTEGER);
                CREATE TABLE IF NOT EXISTS delivery_events (
                  id INTEGER PRIMARY KEY, operation TEXT, sid TEXT, status TEXT, error TEXT, created REAL);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def stopped(self, recipient):
        with self.db() as db:
            row = db.execute("SELECT stopped FROM preferences WHERE recipient=?", (recipient,)).fetchone()
        return bool(row and row[0])

    def send_proposal(self, eid, pid):
        contact = self.contacts.get(eid)
        if not contact:
            raise MessagingError("No explicitly opted-in real recipient is configured for this business")
        recipient = contact["address"]
        if self.stopped(recipient) or self.step5.policy(eid)["opted_out"]:
            raise MessagingError("This owner has opted out; no message sent")
        payload = self.prepare(eid, pid)
        ref = secrets.token_hex(4)
        variables = template_variables(payload, ref)
        if not payload["items"] or any(x["date"] < date.today().isoformat() for x in payload["items"]):
            raise MessagingError("Proposal is empty or contains a past date; no message sent")
        # Snapshot is taken from the real destination and persisted with the exact sent hours.
        payload["seen"] = self.step5.snapshot(eid, [x["date"] for x in payload["items"]])
        if any(payload['seen'].get(x['date']) == x['hours'] for x in payload['items']):
            raise MessagingError('Saved hours changed or already match; refresh the proposal before sending')
        with self.db() as db:
            try:
                db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?,?,NULL)",
                           (ref, eid, pid, recipient, json.dumps(payload), time.time(), "pending"))
            except sqlite3.IntegrityError:
                raise MessagingError("A proposal already exists for this business/period; inspect status instead of resending") from None
        self._send(ref, recipient, variables=variables)
        return {"reference": ref, "status": "submitted", "note": "Delivery is confirmed only by provider callbacks"}

    def _send(self, ref, recipient, *, variables=None, body=None, allow_stopped=False):
        if not allow_stopped and self.stopped(recipient):
            raise MessagingError("Owner opted out before send")
        operation = secrets.token_hex(16)
        with self.db() as db:
            db.execute("INSERT INTO outgoing VALUES(?,?,?,NULL,'submitting',NULL,?)",
                       (operation, ref, recipient, time.time()))
        try:
            sid, status = self.transport.send(recipient, operation, variables=variables, body=body)
        except Exception:
            # A timeout may happen AFTER Twilio accepted a send. Never resend automatically.
            with self.db() as db:
                db.execute("UPDATE outgoing SET state='needs_review', error='Provider request failed or outcome unknown; inspect Twilio logs before retry' WHERE id=? AND state='submitting'", (operation,))
            raise MessagingError("Provider send failed or outcome is unknown; inspect messaging status and Twilio logs") from None
        with self.db() as db:
            # A status callback can arrive before the REST response. Do not regress it.
            db.execute("UPDATE outgoing SET sid=?, state=CASE WHEN state='submitting' THEN ? ELSE state END WHERE id=?",
                       (sid, status, operation))

    def webhook(self, path, raw, signature):
        values = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=100)
        if any(len(v) != 1 for v in values.values()):
            raise MessagingError("Duplicate webhook parameters")
        form = {k: v[0] for k, v in values.items()}
        if not self.transport.validate(path, form, signature) or form.get("AccountSid") != self.transport.account:
            raise PermissionError("Invalid Twilio signature/account")
        route = urlsplit(path).path
        if route == "/messaging/status":
            operation = parse_qs(urlsplit(path).query).get("operation", [""])[0]
            self._status(operation, form)
            return
        if route != "/messaging/inbound" or form.get("To") != self.transport.sender:
            raise PermissionError("Unexpected webhook destination")
        if not SID.fullmatch(form.get("MessageSid", "")):
            raise MessagingError("Missing or invalid MessageSid")
        if form.get("From") not in {c["address"] for c in self.contacts.values()}:
            return  # No hours disclosure or unsolicited response to an unknown sender.
        with self.db() as db:
            inserted = db.execute("INSERT OR IGNORE INTO incoming VALUES(?,?,'pending',?,NULL)",
                                 (form["MessageSid"], json.dumps(form), time.time())).rowcount
            keyword = form.get("Body", "").strip().upper()
            if inserted and keyword in ("STOP", "START"):
                db.execute("INSERT OR REPLACE INTO preferences VALUES(?,?)", (form["From"], int(keyword == "STOP")))

    def _status(self, operation, form):
        status, sid = form.get("MessageStatus", ""), form.get("MessageSid", "")
        ranks = {"accepted": 0, "queued": 1, "sending": 2, "sent": 3, "delivered": 4,
                 "read": 5, "failed": 6, "undelivered": 6, "canceled": 6}
        if status not in ranks or not SID.fullmatch(sid):
            raise MessagingError("Invalid status callback")
        with self.db() as db:
            row = db.execute("SELECT * FROM outgoing WHERE id=?", (operation,)).fetchone()
            if not row or (row["sid"] and row["sid"] != sid):
                raise MessagingError("Unknown outgoing operation")
            db.execute("INSERT INTO delivery_events(operation,sid,status,error,created) VALUES(?,?,?,?,?)",
                       (operation, sid, status, form.get("ErrorCode"), time.time()))
            if ranks[status] >= ranks.get(row["state"], -1):
                db.execute("UPDATE outgoing SET sid=?,state=?,error=? WHERE id=?",
                           (sid, status, form.get("ErrorCode"), operation))

    def process_one(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM incoming WHERE state='pending' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return False
            db.execute("UPDATE incoming SET state='processing' WHERE sid=?", (row["sid"],))
        form = json.loads(row["payload"])
        try:
            if time.time() - row["created"] > 23 * 3600:
                raise MessagingError("Reply window expired; operator review required")
            ref, response = self._reply(form)
            if response:
                self._send(ref, form["From"], body=response,
                           allow_stopped=form.get("Body", "").strip().upper() == "STOP")
            state, error = "done", None
        except Exception:
            state, error = "needs_review", "Decision or reply failed; inspect audit/provider status before recovery"
        with self.db() as db:
            db.execute("UPDATE incoming SET state=?,error=? WHERE sid=?", (state, error, row["sid"]))
        return True

    def _reply(self, form):
        sender, text = form["From"], form.get("ButtonPayload") or form.get("Body", "")
        text = text.strip()
        keyword = text.upper()
        if keyword == "STOP":
            return None, "You have stopped WhatsApp suggestions. Reply START to resume. No hours were changed."
        if keyword == "START":
            return None, "WhatsApp suggestions are enabled again. Your existing business contact restrictions still apply."
        if self.stopped(sender):
            return None, None
        match = re.match(r"^(YES|NO|OTHER|EDIT|UNDO|CONFIRM|CANCEL)\s*[: ]\s*([a-f0-9]{8})(?:\s+([\s\S]*))?$", text, re.I)
        if not match:
            # After Other hours, an authenticated sender can reply naturally.
            # Never guess the business/proposal when more than one is waiting.
            with self.db() as db:
                waiting = [r for r in db.execute("SELECT ref,payload FROM proposals WHERE recipient=? AND state='pending'", (sender,))
                           if json.loads(r['payload']).get('awaiting_edit')]
            if len(waiting) == 1:
                pending = json.loads(waiting[0]['payload']).get('pending_edit')
                action = keyword if keyword in ('CONFIRM', 'CANCEL') else 'EDIT'
                if pending and keyword in ('YES', 'SAVE', 'SAVE THESE HOURS', 'NO'):
                    action = 'CANCEL' if keyword == 'NO' else 'CONFIRM'
                text = action + ' ' + waiting[0]['ref'] + (' ' + text if action == 'EDIT' else '')
                match = re.match(r"^(EDIT|CONFIRM|CANCEL)\s+([a-f0-9]{8})(?:\s+([\s\S]*))?$", text, re.I)
        if not match:
            return None, "Please include the reference from your proposal: YES <reference>, NO <reference>, OTHER <reference>, EDIT <reference> followed by one day per line, or UNDO <reference>. Reply STOP to stop suggestions."
        action, ref, hours = match.group(1).upper(), match.group(2).lower(), match.group(3) or ""
        with self.db() as db:
            proposal = db.execute("SELECT * FROM proposals WHERE ref=? AND recipient=?", (ref, sender)).fetchone()
        if not proposal:
            return None, "That proposal reference is not available for this phone number. No hours were changed."
        eid = proposal["eid"]
        if self.step5.policy(eid)["opted_out"]:
            return ref, "Suggestions are disabled for this business. No hours were changed."
        payload = json.loads(proposal["payload"])
        if action == "UNDO":
            if not proposal["audit_id"]:
                return ref, "There is no recorded change to undo for this proposal."
            result = self.step5.undo(audit_id=proposal["audit_id"], language="en")
            return ref, self._confirmation(result, undo=True)
        if time.time() - proposal["created"] > 7 * 86400 or any(x["date"] < date.today().isoformat() for x in payload["items"]):
            return ref, "This proposal has expired. No hours were changed; please request a fresh review."
        if proposal["state"] in ("done", "declined"):
            return ref, "This proposal already has a decision. No new changes were made. You can UNDO " + ref + " if it was applied."
        if proposal["state"] != "pending":
            return ref, "This proposal needs operator review. No additional change was attempted."
        if action == "OTHER":
            payload['awaiting_edit'] = True
            with self.db() as db:
                db.execute('UPDATE proposals SET payload=? WHERE ref=?', (json.dumps(payload), ref))
            return ref, "No problem! Send the dates and hours you'd like, for example: 24 Dec 10-14. We'll ask you to check them before saving. Days you don't mention stay as they are. Reference: " + ref
        if action == 'CANCEL':
            payload.pop('pending_edit', None)
            with self.db() as db:
                db.execute('UPDATE proposals SET payload=? WHERE ref=?', (json.dumps(payload), ref))
            return ref, 'Cancelled. No hours were changed.'
        if action == "EDIT":
            payload['awaiting_edit'] = True
            found, problems = self.parse_hours(hours, payload["items"])
            payload.pop('pending_edit', None)
            with self.db() as db:
                db.execute('UPDATE proposals SET payload=? WHERE ref=?', (json.dumps(payload), ref))
            if problems or not found:
                return ref, "I couldn't read all those hours, so nothing changed. Reply EDIT " + ref + " followed by one day per line, e.g. 24 Dec 10-14 or 25 Dec closed."
            payload['pending_edit'] = found
            with self.db() as db:
                db.execute('UPDATE proposals SET payload=? WHERE ref=?', (json.dumps(payload), ref))
            return ref, ('Please check these hours:\n' + '\n'.join(d + ': ' + h for d, h in sorted(found.items()))
                         + '\nOther dates stay as they are. Reply CONFIRM ' + ref + ' to save or CANCEL ' + ref + '.')
        elif action == 'CONFIRM':
            found = payload.get('pending_edit')
            if not found:
                return ref, 'There are no hours waiting for confirmation. No hours were changed.'
        elif action == 'YES' and payload.get('pending_edit'):
            return ref, 'You have edited hours waiting for review. Reply CONFIRM ' + ref + ' to save them or CANCEL ' + ref + '.'
        else:
            found = {x["date"]: x["hours"] for x in payload["items"]}
        decisions = [{"date": dt, "decision": "REJECTED" if action == "NO" else "EDITED" if action == "CONFIRM" else "ACCEPTED",
                      "hours": hrs, "seen_before": payload["seen"].get(dt)} for dt, hrs in sorted(found.items())]
        with self.db() as db:
            claimed = db.execute("UPDATE proposals SET state='processing' WHERE ref=? AND state='pending'", (ref,)).rowcount
        if not claimed:
            return ref, "This proposal is being processed or needs operator review. No additional change was attempted."
        result = self.step5.apply({"entity_id": eid, "suggestion_id": "whatsapp-" + ref,
                                  "channel": "WHATSAPP", "actor": "owner", "language": "en", "items": decisions})
        with self.db() as db:
            db.execute("UPDATE proposals SET state=?,audit_id=? WHERE ref=?",
                       ("declined" if action == "NO" else "done", result.get("audit_id"), ref))
        if action == "NO":
            return ref, "OK, we won't change anything."
        return ref, self._confirmation(result) + "\nWrong? Reply UNDO " + ref + "."

    @staticmethod
    def _confirmation(result, undo=False):
        good = [x for x in result.get("items", []) if x["status"] in ("APPLIED", "ALREADY_SET", "UNDONE")]
        bad = [x for x in result.get("items", []) if x["status"] not in ("APPLIED", "ALREADY_SET", "UNDONE")]
        parts = []
        if good:
            parts.append("Verified in the Yext sandbox" + (" (undo)" if undo else "") + ":\n" +
                         "\n".join(x["date"] + ": " + str(x.get("hours") or "regular hours") for x in good))
        if bad:
            parts.append("Not saved/restored: " + ", ".join(x["date"] + " (" + x["status"].lower() + ")" for x in bad) + ". Please ask the operator to review.")
        if not parts:
            parts.append("No hours needed changing.")
        return "\n\n".join(parts) + "\nThis is a sandbox demonstration; live publisher updates are not verified."

    def status(self):
        with self.db() as db:
            return {"outgoing": [dict(r) for r in db.execute("SELECT id,ref,sid,state,error FROM outgoing ORDER BY created")],
                    "incoming": [dict(r) for r in db.execute("SELECT sid,state,error FROM incoming ORDER BY created")],
                    "proposals": [dict(r) for r in db.execute("SELECT ref,eid,pid,state FROM proposals ORDER BY created")]}

    def run(self):
        # A crash during a side effect is uncertain. Never silently replay it.
        with self.db() as db:
            db.execute("UPDATE incoming SET state='needs_review',error='Worker interrupted during processing; inspect audit before recovery' WHERE state='processing'")
        while not self.stop_event.is_set():
            if not self.process_one():
                self.stop_event.wait(0.5)


def from_env(prepare, parse_hours):
    transport = TwilioTransport()
    runtime = Path(os.environ.get("CONNECT_MESSAGING_RUNTIME", Path(__file__).parent / ".runtime" / "messaging"))
    runtime.mkdir(parents=True, exist_ok=True)
    contacts = json.loads(Path(os.environ["CONNECT_MESSAGING_CONTACTS"]).read_text(encoding="utf-8"))
    if not isinstance(contacts, dict):
        raise MessagingError("Contacts file must contain an object keyed by business ID")
    step5 = real_step5(runtime)
    return Messaging(runtime, transport, step5,
                     lambda eid, pid: prepare(eid, pid, source=step5), parse_hours, contacts)
