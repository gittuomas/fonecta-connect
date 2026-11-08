"""Step 5 – Apply: write the owner's decision to Yext, verify it landed, log it, confirm where it shows.

    gate → fresh read → conflict check → ONE read-modify-write PUT → verify → audit → (sandbox mirror) → learn → message

Guarantees (the "reliability" criterion):
- Only an owner's ACCEPTED/EDITED answer, or a standing permission that covers the exact change, can write.
  REJECTED and NO_RESPONSE never write. Permanent closure is not an hours change: FLAG cases never reach this module.
- Never overwrites something the owner changed themselves after the suggestion went out (conflict check).
- Sends the FULL hours object, touching only the approved dates; weekdays and other dates are checked afterwards.
- A 2xx is not trusted: the entity is read back and every date compared (Yext can drop a field with NON_FATAL_ERROR).
- Every write is in the audit log with before/after, so `undo()` can put the old value back.
- Safe to call twice (double tap, retry): a repeat of the same suggestion is answered from the audit log, no 2nd write.
- One write at a time per business (lock), so two answers arriving together can't overwrite each other.

Integration contract (JSON in / JSON out): see step5_apply/INTEGRATION.md.
"""
from __future__ import annotations

import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from . import confirm as C
from . import hours as H
from . import special_days as S
from .store import AuditLog, StandingPermissions
from .yext import YextClient, YextError, fatal_or_field_errors

APPLIED, ALREADY_SET, CONFLICT, DECLINED, NO_RESPONSE, BLOCKED, INVALID, FAILED, UNDONE = (
    "APPLIED", "ALREADY_SET", "CONFLICT", "DECLINED", "NO_RESPONSE", "BLOCKED", "INVALID", "FAILED", "UNDONE")
OWNER_YES = {"ACCEPTED", "EDITED"}
ACTORS = {"owner", "standing_permission"}

# What step 4 might call the owner's answer -> our canonical decision.
DECISION_ALIASES = {
    "ACCEPTED": "ACCEPTED", "ACCEPT": "ACCEPTED", "APPROVED": "ACCEPTED", "APPROVE": "ACCEPTED", "YES": "ACCEPTED",
    "EDITED": "EDITED", "EDIT": "EDITED", "MODIFIED": "EDITED",
    "REJECTED": "REJECTED", "REJECT": "REJECTED", "DECLINED": "REJECTED", "DECLINE": "REJECTED", "NO": "REJECTED",
    "NO_RESPONSE": "NO_RESPONSE", "NONE": "NO_RESPONSE", "TIMEOUT": "NO_RESPONSE", "EXPIRED": "NO_RESPONSE",
}


class _Unset:
    def __repr__(self):
        return "UNSET"


UNSET: Any = _Unset()


def _hours_in(v) -> str:
    """Accept an hours string or a Yext day/holiday object ({"isClosed": true}, {"openIntervals": [...]})."""
    if v is None:
        return ""
    if isinstance(v, dict):
        return H.item_to_str(v) or ""
    return H.normalise(str(v))


@dataclass
class DecisionItem:
    date: str                    # "2026-12-24"
    hours: str                   # final hours: "closed" | "regular" | "10:00-15:00" (the owner's edit if EDITED)
    decision: str = "ACCEPTED"   # ACCEPTED | EDITED | REJECTED | NO_RESPONSE (ignored for standing permission)
    day_key: str = ""            # XMAS_EVE … filled in automatically when empty
    seen_before: Any = UNSET     # the date's holidayHours when the suggestion was sent (None = no entry);
                                 # UNSET skips the conflict check. Get it from Applier.snapshot().

    def __post_init__(self):
        self.hours = _hours_in(self.hours)
        self.decision = DECISION_ALIASES.get(str(self.decision).strip().upper(), str(self.decision))
        if isinstance(self.seen_before, (str, dict)):
            self.seen_before = _hours_in(self.seen_before)

    @classmethod
    def from_dict(cls, d: dict) -> "DecisionItem":
        if not isinstance(d, dict) or not d.get("date"):
            raise ValueError("each item needs a 'date' (YYYY-MM-DD)")
        raw = str(d.get("decision") or "ACCEPTED").strip().upper()
        if raw not in DECISION_ALIASES:
            raise ValueError(f"unknown decision {d.get('decision')!r}; use ACCEPTED, EDITED, REJECTED or NO_RESPONSE")
        hours = d.get("hours", d.get("final_hours", d.get("proposed_hours")))
        seen = UNSET
        if "seen_before" in d:
            seen = None if d["seen_before"] is None else _hours_in(d["seen_before"])
        return cls(str(d["date"]), _hours_in(hours), DECISION_ALIASES[raw], str(d.get("day_key") or ""), seen)


@dataclass
class Decision:
    entity_id: str
    items: list[DecisionItem]
    actor: str = "owner"         # owner | standing_permission
    channel: str = ""
    suggestion_id: str = ""      # the message/suggestion id from step 3; makes repeats safe
    remember_yearly: bool = False
    language: str | None = None  # en | fi | sv | nb | "owner" (= CRM preference); default: Applier.default_language

    @classmethod
    def from_dict(cls, d: dict) -> "Decision":
        """Parse the JSON contract. Accepts {"items": [...]} or a single-date shorthand with the item fields inline."""
        if not isinstance(d, dict):
            raise ValueError("decision must be a JSON object")
        eid = d.get("entity_id") or d.get("entityId")
        if not eid:
            raise ValueError("'entity_id' is required")
        items = d.get("items")
        if items is None:
            items = [d]
        if not isinstance(items, list) or not items:
            raise ValueError("'items' must be a non-empty list")
        parsed = [DecisionItem.from_dict(i) for i in items]
        if len({i.date for i in parsed}) != len(parsed):
            raise ValueError("each date may appear only once per decision")
        actor = d.get("actor", "owner")
        if actor not in ACTORS:
            raise ValueError(f"'actor' must be one of {sorted(ACTORS)}")
        return cls(str(eid), parsed, actor, str(d.get("channel") or ""), str(d.get("suggestion_id") or ""),
                   bool(d.get("remember_yearly", False)), d.get("language"))


@dataclass
class ItemResult:
    date: str
    status: str
    hours: str | None = None     # the value we wanted in Yext
    before: str | None = None    # the value Yext had
    reason: str = ""
    duplicate: bool = False      # repeat of a request that was already applied


@dataclass
class ApplyResult:
    entity_id: str
    items: list[ItemResult] = field(default_factory=list)
    audit_id: str | None = None
    message: str | None = None   # text for the owner; None = send nothing
    mirror: dict | None = None   # sandbox write result, when mirrored
    message_short: str | None = None   # one-line version for SMS / WhatsApp previews

    @property
    def applied(self) -> list[ItemResult]:
        return [i for i in self.items if i.status in (APPLIED, UNDONE)]

    def to_dict(self) -> dict:
        return {**asdict(self), "applied": [i.date for i in self.applied]}


class Applier:
    def __init__(self, primary: YextClient, audit: AuditLog, permissions: StandingPermissions,
                 contacts: dict[str, dict] | None = None, sandbox: YextClient | None = None,
                 mirror: dict[str, str] | None = None, today: date | None = None, default_language: str = "en"):
        self.primary, self.audit, self.permissions = primary, audit, permissions
        self.default_language = default_language   # team decision: English for the demo; "owner" = CRM language
        self.contacts = contacts or {}
        self.sandbox, self.mirror_map = sandbox, mirror or {}
        self.today = today
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._sandbox_lock = threading.Lock()

    def _today(self) -> date:
        if self.today:
            return self.today
        env = os.environ.get("CONNECT_TODAY")
        return date.fromisoformat(env) if env else date.today()

    def _lang(self, requested: str | None, entity_id: str) -> str | None:
        want = requested or self.default_language
        if want in (None, "", "owner"):
            return self.contacts.get(entity_id, {}).get("preferred_language")
        return want

    def _lock(self, entity_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(entity_id, threading.Lock())

    # ================================================================== helpers for steps 2–4
    def snapshot(self, entity_id: str, dates: list[str]) -> dict[str, str | None]:
        """What Yext shows for these dates right now. Store it with the suggestion and send it back as
        `seen_before`, so step 5 can tell if the owner changed the date themselves in the meantime."""
        current = H.holiday_map(self.primary.get_entity(entity_id).get("hours", {}))
        return {d: current.get(d) for d in dates}

    def policy(self, entity_id: str, day_key: str = "", hours: str | None = None, date_: str = "") -> dict:
        """For step 3: may this change be applied without asking (then inform), or must we ask?"""
        c = self.contacts.get(entity_id, {})
        key = day_key or (S.day_key(entity_id, date_) if date_ else "")
        learned = self.permissions.lookup(entity_id, key) if key else None
        opted_out = c.get("opted_out_of_suggestions") == "1"
        general = c.get("standing_permission_holidays") == "1"
        covers = bool(learned) and hours is not None and learned["hours"] == _hours_in(hours)
        return {"entity_id": entity_id, "day_key": key, "opted_out": opted_out,
                "standing_permission_general": general, "learned_permission": learned,
                "auto_apply": (not opted_out) and (general or covers),
                "preferred_channel": c.get("preferred_channel"), "preferred_language": c.get("preferred_language"),
                "best_contact_window": c.get("best_contact_window")}

    # ================================================================== apply
    def apply(self, d: Decision) -> ApplyResult:
        with self._lock(d.entity_id):
            return self._apply(d)

    def _apply(self, d: Decision) -> ApplyResult:
        eid = d.entity_id
        contact = self.contacts.get(eid, {})
        lang = self._lang(d.language, eid)
        for it in d.items:
            if not it.day_key:
                it.day_key = S.day_key(eid, it.date)
        out: dict[str, ItemResult] = {}

        # 0. gate
        todo = []
        for it in d.items:
            r = self._gate(d, it, contact)
            if r:
                out[it.date] = r
            else:
                todo.append(it)

        ent, writes, prior_ids = None, {}, set()
        records = self.audit.all() if todo and d.suggestion_id else []   # read the log once per call
        if todo:
            # 1. fresh read
            try:
                ent = self.primary.get_entity(eid)
            except YextError as e:
                for it in todo:
                    out[it.date] = ItemResult(it.date, FAILED, it.hours, reason=f"read failed: {e}")
                todo = []
            # 2. conflict check (+ repeat detection)
            current = H.holiday_map((ent or {}).get("hours", {}))
            for it in todo:
                want, cur = it.hours, current.get(it.date)
                seen = it.seen_before
                if cur == want:
                    prior = self._prior_apply(eid, d.suggestion_id, it.date, want, records)
                    if prior:
                        prior_ids.add(prior)
                        out[it.date] = ItemResult(it.date, ALREADY_SET, want, cur,
                                                  f"repeat of an applied request (audit {prior})", duplicate=True)
                    else:
                        out[it.date] = ItemResult(it.date, ALREADY_SET, want, cur, "Yext already shows this")
                elif seen is not UNSET and cur != seen:
                    out[it.date] = ItemResult(it.date, CONFLICT, want, cur, "changed after the suggestion was sent")
                else:
                    writes[it.date] = (it, cur)

        audit_id = mirror = None
        if writes:
            # 3–5. one read-modify-write PUT, then verify
            res, req_id = self._write_verify(self.primary, eid, ent.get("hours", {}),
                                             {dt: it.hours for dt, (it, _) in writes.items()})
            for dt, (it, cur) in writes.items():
                ok, why = res[dt]
                out[dt] = ItemResult(dt, APPLIED if ok else FAILED, it.hours, cur, why)
            done = {dt: out[dt].hours for dt in writes if out[dt].status == APPLIED}
            if done:
                mirror = self._mirror(eid, self._final(out))
                by_date = {it.date: it for it in d.items}
                # 6. audit
                audit_id = self.audit.append({
                    "type": "APPLY", "entity_id": eid, "suggestion_id": d.suggestion_id, "actor": d.actor,
                    "channel": d.channel, "target": self.primary.name, "request_id": req_id, "mirror": mirror,
                    "items": [{"date": dt, "day_key": by_date[dt].day_key, "decision": by_date[dt].decision,
                               "before": out[dt].before, "after": h} for dt, h in done.items()]})
        if audit_id is None and self._final(out):
            # Nothing new written to the primary, but the sandbox may still lag (e.g. its write failed the first time
            # and this is the retry). _mirror only writes the dates the sandbox doesn't show yet.
            mirror = self._mirror(eid, self._final(out))
            if mirror and mirror.get("written"):
                self.audit.append({"type": "MIRROR", "entity_id": eid, "suggestion_id": d.suggestion_id,
                                   "actor": d.actor, "channel": d.channel, "target": self.sandbox.name,
                                   "mirror": mirror})
        if audit_id is None and len(prior_ids) == 1:
            audit_id = prior_ids.pop()   # a pure repeat: hand back the original record (e.g. for undo)

        # 8. learn: "closed every Christmas Eve, don't ask again"
        if d.remember_yearly and d.actor == "owner":
            for it in d.items:
                if it.day_key not in S.NOT_YEARLY and out[it.date].status in (APPLIED, ALREADY_SET):
                    self.permissions.grant(eid, it.day_key, it.hours, source=d.suggestion_id or "owner")

        items = [out[it.date] for it in d.items]
        name = (ent or {}).get("name", eid)
        msg = self._message(d, name, items, lang)
        applied = [(r.date, r.hours) for r in items if r.status == APPLIED]
        short = C.applied_message_short(name, applied, eid, lang) if applied else msg
        return ApplyResult(eid, items, audit_id, msg, mirror, short)

    @staticmethod
    def _final(out: dict[str, ItemResult]) -> dict[str, str]:
        """Dates whose primary value is now the approved one (just written, or already there)."""
        return {dt: r.hours for dt, r in out.items() if r.status in (APPLIED, ALREADY_SET)}

    def _prior_apply(self, eid: str, suggestion_id: str, day: str, hours: str, records: list[dict]) -> str | None:
        if not suggestion_id:
            return None
        for r in reversed(records):
            if r.get("type") == "APPLY" and r["entity_id"] == eid and r.get("suggestion_id") == suggestion_id and \
                    any(i["date"] == day and i["after"] == hours for i in r["items"]) and \
                    day not in self.audit.undone_dates(r["id"], records):
                return r["id"]
        return None

    def _gate(self, d: Decision, it: DecisionItem, contact: dict) -> ItemResult | None:
        want = it.hours
        if contact.get("opted_out_of_suggestions") == "1":
            return ItemResult(it.date, BLOCKED, want, reason="owner opted out of suggestions")
        if d.actor == "owner":
            if it.decision == "REJECTED":
                return ItemResult(it.date, DECLINED, want, reason="owner said no")
            if it.decision == "NO_RESPONSE":
                return ItemResult(it.date, NO_RESPONSE, want, reason="no answer means no change")
            if it.decision not in OWNER_YES:
                return ItemResult(it.date, BLOCKED, want, reason=f"unknown decision {it.decision!r}")
        elif d.actor == "standing_permission":
            general = contact.get("standing_permission_holidays") == "1"
            learned = bool(it.day_key) and self.permissions.covers(d.entity_id, it.day_key, want)
            if not (general or learned):
                return ItemResult(it.date, BLOCKED, want, reason="no standing permission covers this change")
        else:
            return ItemResult(it.date, BLOCKED, want, reason=f"unknown actor {d.actor!r}")
        if not want:
            return ItemResult(it.date, INVALID, want, reason="hours missing")
        errs = H.validate(want)
        if errs:
            return ItemResult(it.date, INVALID, want, reason="; ".join(errs))
        try:
            day = date.fromisoformat(it.date)
        except ValueError:
            return ItemResult(it.date, INVALID, want, reason="bad date")
        if day < self._today():
            return ItemResult(it.date, BLOCKED, want, reason="date has already passed")
        return None

    # ================================================================== write + verify
    def _write_verify(self, client: YextClient, eid: str, hours: dict, changes: dict[str, str | None]):
        """PUT the full hours object with `changes` applied, read back, check each date and that nothing else moved.

        Returns ({date: (ok, reason)}, request_id).
        - A refused PUT (HTTP status) saved nothing. A PUT with an unknown outcome (network error, unreadable
          response) is read back, so a change that did land is reported (and audited) as applied.
        - Holiday dates in the past are carried along unchanged, but if Yext refuses the PUT they are dropped and
          the PUT is retried once: they no longer show anywhere and Yext may not accept them. They are also left
          out of the collateral check, in case Yext prunes them by itself.
        """
        today = self._today().isoformat()
        new = H.with_holiday_changes(hours, changes)
        env, put_err, dropped = {}, None, set()
        try:
            env = client.update_entity(eid, {"hours": new})
        except YextError as e:
            put_err = e
            stale = {h["date"] for h in new.get("holidayHours", []) if h["date"] < today and h["date"] not in changes}
            if e.status is not None and 400 <= e.status < 500 and e.status != 429 and stale:
                dropped = stale
                new = H.with_holiday_changes(new, {dt: None for dt in stale})
                try:
                    env, put_err = client.update_entity(eid, {"hours": new}), None
                except YextError as e2:
                    put_err = e2
            if put_err is not None and put_err.status is not None:
                return {dt: (False, f"write failed: {put_err}") for dt in changes}, None
        req_id = env.get("meta", {}).get("uuid")
        errs = fatal_or_field_errors(env)
        try:
            after = client.get_entity(eid).get("hours", {})
        except YextError as e:
            why = f"write outcome unknown ({put_err})" if put_err else "written"
            return {dt: (False, f"{why} but could not verify: {e}") for dt in changes}, req_id

        got = H.holiday_map(after)
        res = {}
        for dt, s in changes.items():
            want = None if s is None else H.normalise(s)
            if got.get(dt) == want:
                res[dt] = (True, "")
            else:
                hint = f" ({errs[0].get('message')})" if errs else f" ({put_err})" if put_err else ""
                res[dt] = (False, f"verify: Yext shows {got.get(dt)!r}, expected {want!r}{hint}")

        # collateral check: weekdays and untouched upcoming dates must be exactly as before
        def others(m: dict) -> dict:
            return {k: v for k, v in m.items() if k not in changes and k >= today}

        if H.weekdays(after) != H.weekdays(hours) or others(got) != others(H.holiday_map(hours)):
            # Always send holidayHours explicitly (even []): Yext omits the key on read when there are none, and a PUT
            # without it keeps the existing dates (verified on the sandbox), so the new date would survive the restore.
            restore = H.with_holiday_changes(hours, {dt: None for dt in dropped})
            try:
                client.update_entity(eid, {"hours": restore})      # put everything back
                note = "other hours changed unexpectedly; original hours restored"
            except YextError as e:
                note = f"other hours changed unexpectedly and restore failed: {e}"
            res = {dt: (False, note) for dt in changes}
        return res, req_id

    def _mirror(self, eid: str, changes: dict[str, str | None], expected: dict | None = None) -> dict | None:
        """Bring the paired sandbox business to the same values for these dates (demo: real Yext write).

        Writes only the dates the sandbox doesn't already show, so calling it again after a failed mirror is safe
        and retries just what is missing. Never blocks the main result."""
        sid = self.mirror_map.get(eid)
        if not (self.sandbox and sid and changes):
            return None
        with self._sandbox_lock:
            try:
                hours = self.sandbox.get_entity(sid).get("hours", {})
            except YextError as e:
                return {"entity_id": sid, "target": self.sandbox.name, "ok": False, "written": False, "reason": str(e)}
            cur = H.holiday_map(hours)
            todo = {dt: v for dt, v in changes.items() if cur.get(dt) != (None if v is None else H.normalise(v))}
            conflicts = {dt: 'sandbox date changed after our write; left as is' for dt in todo
                         if expected is not None and cur.get(dt) != expected.get(dt)}
            todo = {dt: value for dt, value in todo.items() if dt not in conflicts}
            before = {dt: cur.get(dt) for dt in todo}
            if not todo:
                return {"entity_id": sid, "target": self.sandbox.name, "ok": not conflicts, "written": False,
                        "before": {}, "after": {}, "errors": conflicts, "request_id": None}
            res, req_id = self._write_verify(self.sandbox, sid, hours, todo)
        return {"entity_id": sid, "target": self.sandbox.name, "ok": not conflicts and all(ok for ok, _ in res.values()),
                "written": any(ok for ok, _ in res.values()), "before": before,
                "after": {dt: todo[dt] for dt, (ok, _) in res.items() if ok},
                "errors": {**conflicts, **{dt: why for dt, (ok, why) in res.items() if not ok}}, "request_id": req_id}

    def _restore_mirror(self, rec: dict, dates: list[str]) -> dict | None:
        """Restore the sandbox's own before-values, including a successful mirror retry.

        The paired demo entity can have different exceptions from the mock. Dates
        already correct in the sandbox were never ours to undo; later edits also win.
        """
        mirrors = [rec.get('mirror') or {}]
        mirrors += [r.get('mirror') or {} for r in self.audit.all()
                    if r.get('type') == 'MIRROR' and r.get('entity_id') == rec['entity_id']
                    and rec.get('suggestion_id') and r.get('suggestion_id') == rec['suggestion_id']]
        before, after, target = {}, {}, None
        original = {i['date']: i['after'] for i in rec['items']}
        for m in mirrors:
            if not m.get('written'):
                continue
            for dt, value in m.get('before', {}).items():
                if dt in dates and dt not in m.get('errors', {}):
                    before.setdefault(dt, value)
                    after[dt] = m.get('after', original).get(dt)
                    target = m['entity_id']
        if not before:
            return None
        if self.mirror_map.get(rec['entity_id']) != target:
            return {'entity_id': target, 'ok': False, 'written': False,
                    'reason': 'Sandbox mapping changed; restore the original mapping before undo'}
        return self._mirror(rec['entity_id'], before, expected=after)

    # ================================================================== undo
    def latest_apply(self, entity_id: str) -> str | None:
        """The business's most recent APPLY, if it hasn't been undone yet (for an owner who just replies UNDO).

        Only ever the latest change: a second UNDO must not silently roll back an older, unrelated change."""
        records = self.audit.all()
        for r in reversed(records):
            if r.get("type") == "APPLY" and r["entity_id"] == entity_id:
                return None if self.audit.undone(r["id"], records) else r["id"]
        return None

    def undo(self, audit_id: str, language: str | None = None) -> ApplyResult:
        rec = self.audit.get(audit_id)
        if not rec or rec.get("type") != "APPLY":
            raise ValueError(f"no APPLY record {audit_id!r}")
        with self._lock(rec["entity_id"]):
            return self._undo(rec, language)

    def _undo(self, rec: dict, language: str | None) -> ApplyResult:
        """Put back the values from before an APPLY, but only for dates nobody has changed since.

        Dates that an earlier (partial) undo already put back are skipped, so a date whose undo failed can be retried."""
        audit_id, eid = rec["id"], rec["entity_id"]
        lang = self._lang(language, eid)
        done_before = self.audit.undone_dates(audit_id)
        if all(i["date"] in done_before for i in rec["items"]):
            return ApplyResult(eid, [ItemResult(i["date"], BLOCKED, i["before"], i["after"], "already undone")
                                     for i in rec["items"]])
        try:
            ent = self.primary.get_entity(eid)
        except YextError as e:
            return ApplyResult(eid, [ItemResult(i["date"], FAILED, i["before"], i["after"], f"read failed: {e}")
                                     for i in rec["items"]])
        cur = H.holiday_map(ent.get("hours", {}))
        out, changes = {}, {}
        for i in rec["items"]:
            if i["date"] in done_before:
                out[i["date"]] = ItemResult(i["date"], BLOCKED, i["before"], i["after"], "already undone")
            elif cur.get(i["date"]) == i["after"]:
                changes[i["date"]] = i["before"]
            else:
                out[i["date"]] = ItemResult(i["date"], CONFLICT, i["before"], cur.get(i["date"]),
                                            "changed again since; left as is")
        new_id = mirror = None
        if changes:
            res, req_id = self._write_verify(self.primary, eid, ent.get("hours", {}), changes)
            for dt, (ok, why) in res.items():
                out[dt] = ItemResult(dt, UNDONE if ok else FAILED, changes[dt], cur.get(dt), why)
            undone = [dt for dt in changes if out[dt].status == UNDONE]
            if undone:
                mirror = self._restore_mirror(rec, undone)
                new_id = self.audit.append({
                    "type": "UNDO", "undo_of": audit_id, "entity_id": eid, "target": self.primary.name,
                    "request_id": req_id, "mirror": mirror,
                    "items": [{"date": dt, "before": cur.get(dt), "after": changes[dt]} for dt in undone]})
        items = [out[i["date"]] for i in rec["items"]]
        msg = "\n".join(C.status_message("undone", ent.get("name", eid), r.date, r.hours, lang)
                        for r in items if r.status == UNDONE) or None
        return ApplyResult(eid, items, new_id, msg, mirror, msg)

    # ================================================================== message
    def _message(self, d: Decision, name: str, items: list[ItemResult], lang: str | None) -> str | None:
        parts = []
        applied = [(r.date, r.hours) for r in items if r.status == APPLIED]
        if applied:
            parts.append(C.applied_message(name, applied, d.entity_id, lang, auto=d.actor == "standing_permission"))
        already = [r for r in items if r.status == ALREADY_SET and not r.duplicate]
        if already and lang == 'en':
            parts.append("These hours are already up to date. No new changes were needed:\n" +
                         '\n'.join(f"• {C.day_label(r.date, lang)}: {C.hours_label(r.hours, lang)}" for r in already))
        for r in items:
            if r.status == ALREADY_SET and not r.duplicate and lang != 'en':
                parts.append(C.status_message("already", name, r.date, r.hours, lang))
            elif r.status == CONFLICT:
                parts.append(C.status_message("conflict", name, r.date, r.before, lang))
            elif r.status == FAILED:
                parts.append(C.status_message("failed", name, r.date, r.hours, lang))
        return "\n\n".join(parts) or None
