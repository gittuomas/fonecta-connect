"""Learning what each owner wants (see docs/new-customer-plan.md).

We don't ask a new customer "what content do you want?" (too abstract). Instead every suggestion has a
reason (public holiday, local event, ...), and each owner gets a score per reason from what they do:

    yes / own hours  +1      no  -1.5      no reply (after the reminder)  -0.3      UNDO  -1.5

- A reason at -2.5 or lower stops being sent to that owner. Public holidays are never switched off.
- After 3 unanswered messages in a row the owner is in quiet mode: at most 3 messages a year, the ones
  with the biggest impact (their own Google views on those days last year), first on the owner's preferred
  channel, then the usual WhatsApp reminder. A yes or own hours ends quiet mode; a "No" keeps it (3 a year) but
  the channel the owner answered on is used from then on.
- After a "no", the owner is asked once per reason: "Would you like suggestions like this in the future?"
  "No, not these" switches that reason off (the owner's choice wins, even for public holidays);
  "Yes, keep sending" cancels the -1.5 from the no.
- STOP: only critical messages ("Are you still open?"). START turns suggestions back on.
- Critical messages are always sent, in every mode.

State is saved in app/.runtime/learning.json (gitignored). Standard library; pyarrow is used for
the impact ranking when it's installed (otherwise days are ranked by type only).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime
from functools import lru_cache

HERE = os.path.dirname(os.path.abspath(__file__))
MAT = os.path.join(os.path.dirname(HERE), "resources")
STATE = os.path.join(HERE, ".runtime", "learning.json")

# ------------------------------------------------------------------ reasons

REASONS = {
    "holiday": "Public holidays",
    "eve": "Holiday eves and days between",
    "theme": "Shopping and theme days",
    "event": "Local events",
    "school": "School holidays",
    "weather": "Weather",
    "season": "Season start and end",
}
NEVER_OFF = {"holiday"}          # public holidays always matter
WEIGHT = {"holiday": 2.0, "eve": 1.5, "theme": 1.0, "event": 1.2, "school": 1.0, "weather": 1.0, "season": 2.0}

_BY_KEY = {
    "EVENT": "event", "SCHOOL_BREAK": "school", "WEATHER": "weather", "SEASON": "season",
    "XMAS_EVE": "holiday", "XMAS_DAY": "holiday", "BOXING_DAY": "holiday", "NEW_YEARS_DAY": "holiday",
    "EPIPHANY": "holiday", "INDEPENDENCE_DAY": "holiday",
    "DEC23": "eve", "NYE": "eve", "EPIPHANY_EVE": "eve", "INTERDAY": "eve",
    "BLACK_FRIDAY": "theme", "FATHERS_DAY": "theme", "LUCIA": "theme",
}
_BY_CLASS = {"MAJOR": "holiday", "EVE_MAJOR": "eve", "EVE_MINOR": "eve", "INTER": "eve",
             "COMM": "theme", "OBS": "theme", "SOFT": "theme"}


@lru_cache(maxsize=1)
def _day_classes():
    import csv
    with open(os.path.join(MAT, "data", "calendar_special_days.csv"), encoding="utf-8") as f:
        return {r["day_key"]: r["day_class"] for r in csv.DictReader(f)}


def reason_of(day_key: str) -> str:
    """Which reason a suggested day belongs to."""
    return _BY_KEY.get(day_key) or _BY_CLASS.get(_day_classes().get(day_key, ""), "theme")


# ------------------------------------------------------------------ scoring rules

CHANGE = {"YES": 1.0, "EDIT": 1.0, "NO": -1.5, "NO_REPLY": -0.3, "UNDO": -1.5}
OFF_AT = -2.5          # a reason at or below this score is no longer sent
QUIET_AFTER = 3        # unanswered messages in a row before quiet mode (and only once every channel was tried)
QUIET_PER_YEAR = 3     # messages a year in quiet mode ("Are you still open?" doesn't count)

# ------------------------------------------------------------------ channel: learned from replies, never asked
# Silence on one channel -> try the next one before going quiet. At most 3 channels: the owner's preferred one,
# then WhatsApp, then SMS (2025 pilot: WhatsApp 61% yes, SMS 46%, email 40%, app push 32%). Email or app push
# are only used when the owner chose them. New owners (never replied) move on after 1 unanswered message;
# owners who have replied before after 2 (one silence can just be a busy week). A reply on a channel makes it
# the owner's channel.
LADDER = ["WHATSAPP", "SMS"]
MAX_CHANNELS = 3
SWITCH_AFTER = {"new": 1, "known": 2}
REPLIES = {"YES", "EDIT", "NO", "UNDO"}


# ------------------------------------------------------------------ impact: the owner's own Google views

@lru_cache(maxsize=256)
def _views(eid: str) -> dict:
    """date -> Google search + map views for one business (empty if pyarrow isn't installed)."""
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return {}
    t = pq.read_table(os.path.join(MAT, "data", "analytics_daily.parquet"),
                      columns=["date", "GOOGLE_SEARCH_VIEWS", "GOOGLE_MAP_VIEWS"],
                      filters=[("entity_id", "=", eid)]).to_pydict()
    out = {}
    for d, s, m in zip(t["date"], t["GOOGLE_SEARCH_VIEWS"], t["GOOGLE_MAP_VIEWS"]):
        k = d.isoformat()[:10] if hasattr(d, "isoformat") else str(d)[:10]
        out[k] = (s or 0) + (m or 0)
    return out


def views_last_year(eid: str, day: date) -> int:
    try:
        last = day.replace(year=day.year - 1)
    except ValueError:            # 29 Feb
        last = day.replace(year=day.year - 1, day=28)
    return int(_views(eid).get(last.isoformat(), 0))


def impact(eid: str, days: list, wrong: bool = True) -> float:
    """How much a message matters: Google views on those days last year, weighted by the kind of day.
    days: [(date, day_key)]. wrong: Google would show the wrong hours without a change."""
    total = 0.0
    for d, key in days:
        total += (views_last_year(eid, d) + 10) * WEIGHT[reason_of(key)]   # +10 so new businesses still rank by type
    return total * (1.5 if wrong else 1.0)


# ------------------------------------------------------------------ saved state

class Learner:
    def __init__(self, path: str = STATE, preferred=None):
        self.path = path
        self._lock = threading.Lock()
        self.preferred = preferred or (lambda eid: None)   # eid -> preferred_channel from customer_contacts.csv

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        with open(self.path, encoding="utf-8") as f:
            return json.load(f) or {}

    def _save(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    @staticmethod
    def _owner(data: dict, eid: str) -> dict:
        o = data.setdefault(eid, {"scores": {}, "silent": 0, "stopped": False, "history": []})
        o.setdefault("off", [])      # reasons the owner said "No, not these" to
        o.setdefault("asked", [])    # reasons we've already asked "suggestions like this in the future?" about
        o.setdefault("unanswered", {})   # channel -> unanswered messages on it since its last reply
        o.setdefault("replied_on", None)  # the channel the owner last answered on
        o.setdefault("quiet", False)      # in quiet mode (kept after a "No"; only a yes or own hours ends it)
        return o

    def owner(self, eid: str) -> dict:
        with self._lock:
            o = self._load().get(eid) or {"scores": {}, "silent": 0, "stopped": False, "history": []}
        o.setdefault("off", [])
        o.setdefault("asked", [])
        o.setdefault("unanswered", {})
        o.setdefault("replied_on", None)
        o.setdefault("quiet", False)
        return o

    # -------------------------------------------------------------- what the owner did
    def record(self, eid: str, day_keys, outcome: str, message: str = "", channel: str | None = None) -> dict:
        """outcome: YES | EDIT | NO | NO_REPLY | UNDO. day_keys: the days in the message.
        channel: where the message went (a NO_REPLY without one counts against the channel Connect would use)."""
        if outcome not in CHANGE:
            raise ValueError(f"unknown outcome {outcome}")
        reasons = sorted({reason_of(k) for k in day_keys})
        if outcome == "NO_REPLY" and channel is None:
            channel = self.channel(eid)[0]
        was_quiet = self.mode(eid) == "quiet"
        with self._lock:
            data = self._load()
            o = self._owner(data, eid)
            for r in reasons:
                o["scores"][r] = round(o["scores"].get(r, 0.0) + CHANGE[outcome], 2)
            o["silent"] = o["silent"] + 1 if outcome == "NO_REPLY" else 0
            # Quiet mode ends only with a yes or own hours. A "No" (or UNDO) from a quiet owner keeps them quiet
            # (still at most 3 messages a year), but the channel they answered on becomes their channel.
            if outcome in ("YES", "EDIT"):
                o["quiet"] = False
            elif was_quiet:
                o["quiet"] = True
            if channel and outcome == "NO_REPLY":
                o["unanswered"][channel] = o["unanswered"].get(channel, 0) + 1
            elif channel and outcome in REPLIES:
                o["replied_on"] = channel
                o["unanswered"][channel] = 0
            o["history"].append({"at": datetime.now().isoformat(timespec="seconds"), "message": message,
                                 "outcome": outcome, "reasons": reasons, "channel": channel})
            self._save(data)
            return o

    # -------------------------------------------------------------- which channel next
    def ladder(self, eid: str) -> list:
        """Channels Connect may try for this owner, in order (at most MAX_CHANNELS)."""
        out = []
        for ch in [self.preferred(eid)] + LADDER:
            if ch and ch not in out:
                out.append(ch)
        return out[:MAX_CHANNELS]

    def is_new(self, eid: str) -> bool:
        """Never replied to anything yet: we don't know which channel works for them."""
        return not any(x.get("outcome") in REPLIES for x in self.owner(eid)["history"])

    def channel(self, eid: str) -> tuple:
        """(channel for the next message, why, every channel tried?)."""
        o = self.owner(eid)
        ladder = self.ladder(eid)
        if o["quiet"] and o["replied_on"]:   # quiet owner who answered "No": keep using that channel
            ch = o["replied_on"]
            how = _label(ch) + (" + WhatsApp reminder" if ch != "WHATSAPP" else "")
            return ch, f"Quiet mode: {how} (answered there before)", True
        if o["replied_on"]:                  # the channel they actually answered on goes first
            ladder = [o["replied_on"]] + [c for c in ladder if c != o["replied_on"]]
        limit = SWITCH_AFTER["new" if self.is_new(eid) else "known"]
        for i, ch in enumerate(ladder):
            if o["unanswered"].get(ch, 0) >= limit:
                continue
            if ch == o["replied_on"]:
                return ch, f"Answered on {_label(ch)} before", False
            skipped = [c for c in ladder[:i] if o["unanswered"].get(c, 0) >= limit]
            if not skipped:
                return ch, "Preferred channel" if ch == self.preferred(eid) else "Best channel so far", False
            return ch, f"No reply on {', '.join(_label(c) for c in skipped)}: trying {_label(ch)}", False
        # Quiet mode: first message on the channel the owner chose (WhatsApp if none), and every message still
        # gets its WhatsApp reminder, so each one reaches both. Simulated on the challenge's 819 owners: about as
        # good as WhatsApp-only when owners have no favourite channel (73% vs 76% brought back), much better
        # when some do (76-77% vs 62-65%). Rotating channels came out in between.
        best = o["replied_on"] or self.preferred(eid) or LADDER[0]
        how = _label(best) + (" + WhatsApp reminder" if best != "WHATSAPP" else "")
        return best, f"Quiet mode: {how} (no reply on {', '.join(_label(c) for c in ladder)})", True

    def to_ask(self, eid: str, day_keys) -> list:
        """Reasons in this message we haven't yet asked "suggestions like this in the future?" about."""
        o = self.owner(eid)
        return sorted({reason_of(k) for k in day_keys} - set(o["asked"]))

    def set_wanted(self, eid: str, reasons, keep: bool) -> None:
        """Owner's answer to "Would you like suggestions like this in the future?"."""
        with self._lock:
            data = self._load()
            o = self._owner(data, eid)
            for r in reasons:
                if r not in o["asked"]:
                    o["asked"].append(r)
                if keep:
                    if r in o["off"]:
                        o["off"].remove(r)
                    o["scores"][r] = round(o["scores"].get(r, 0.0) - CHANGE["NO"], 2)   # cancel the no
                elif r not in o["off"]:
                    o["off"].append(r)
            o["history"].append({"at": datetime.now().isoformat(timespec="seconds"),
                                 "outcome": "KEEP" if keep else "NOT_THESE", "reasons": sorted(reasons)})
            self._save(data)

    def set_stopped(self, eid: str, stopped: bool) -> None:
        with self._lock:
            data = self._load()
            o = self._owner(data, eid)
            o["stopped"] = stopped
            if not stopped:
                o["off"] = []          # START: every kind of suggestion is back on, and quiet mode ends
                o["quiet"], o["silent"] = False, 0
            o["history"].append({"at": datetime.now().isoformat(timespec="seconds"),
                                 "outcome": "STOP" if stopped else "START", "reasons": []})
            self._save(data)

    def reset(self, eid: str | None = None) -> None:
        with self._lock:
            data = self._load()
            if eid is None:
                data = {}
            else:
                data.pop(eid, None)
            self._save(data)

    # -------------------------------------------------------------- deciding
    def mode(self, eid: str) -> str:
        o = self.owner(eid)
        if o["stopped"]:
            return "stopped"
        # Quiet after several silences once every channel on the ladder was tried; it stays on after a "No".
        if o["quiet"]:
            return "quiet"
        return "quiet" if o["silent"] >= QUIET_AFTER and self.channel(eid)[2] else "normal"

    def reason_on(self, eid: str, reason: str) -> bool:
        o = self.owner(eid)
        if reason in o["off"]:            # the owner said "No, not these": always respected
            return False
        return reason in NEVER_OFF or o["scores"].get(reason, 0.0) > OFF_AT

    def plan(self, eid: str, messages: list) -> dict:
        """Which of this season's messages to send. messages: [{"id", "days": [(date, day_key)],
        "critical": bool, "wrong": bool}] -> {id: (send, why)}."""
        mode = self.mode(eid)
        out, candidates = {}, []
        for m in messages:
            reasons = {reason_of(k) for _, k in m["days"]}
            if m.get("critical"):
                out[m["id"]] = (True, "Always sent: something looks clearly wrong")
            elif mode == "stopped":
                out[m["id"]] = (False, "Owner replied STOP: only critical messages")
            elif not any(self.reason_on(eid, r) for r in reasons):
                names = ", ".join(REASONS[r].lower() for r in sorted(reasons))
                out[m["id"]] = (False, f"Learned: this owner doesn't want {names}")
            else:
                candidates.append(m)
        if mode == "quiet":
            ranked = sorted(candidates, key=lambda m: impact(eid, m["days"], m.get("wrong", True)), reverse=True)
            for i, m in enumerate(ranked):
                out[m["id"]] = ((True, f"Quiet mode: top {QUIET_PER_YEAR} by impact (#{i + 1})") if i < QUIET_PER_YEAR
                                else (False, f"Quiet mode: not in the top {QUIET_PER_YEAR} this year"))
        else:
            for m in candidates:
                out[m["id"]] = (True, "Will be sent")
        return out

    def summary(self, eid: str) -> str:
        """One line for the Connect panel."""
        o, mode = self.owner(eid), self.mode(eid)
        label = {"normal": "Normal", "quiet": f"Quiet mode (max {QUIET_PER_YEAR}/year)",
                 "stopped": "STOP: critical only"}[mode]
        scores = ", ".join(f"{REASONS[r]} {v:+g}{' (off)' if not self.reason_on(eid, r) else ''}"
                           for r, v in sorted(o["scores"].items()) if r not in o["off"])
        if o["off"]:
            scores += ("; " if scores else "") + "Owner said no to: " + ", ".join(REASONS[r].lower() for r in o["off"])
        silent = f" · {o['silent']} unanswered in a row" if o["silent"] and mode != "quiet" else ""
        ch, why, _ = self.channel(eid)
        nxt = f" · Next message: {_label(ch)} ({why}{', new customer' if self.is_new(eid) else ''})"
        return f"{label}{silent}{nxt}" + (f" · Learned: {scores}" if scores else "")


def _label(ch: str) -> str:
    return {"WHATSAPP": "WhatsApp", "SMS": "SMS", "EMAIL": "Email", "APP_PUSH": "App push"}.get(ch, ch)
