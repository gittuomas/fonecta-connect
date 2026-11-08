"""File-backed state: call sessions, outreach history, standing permissions, and the audit trail.

state.json holds the mutable state; decisions.jsonl and events.jsonl are append-only so the
audit trail survives restarts and can be read by the learning code next to the WhatsApp log.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config

_LOCK = threading.RLock()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or config.DATA_DIR)
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"
        self.decisions_path = self.root / "decisions.jsonl"
        self.events_path = self.root / "events.jsonl"
        self.state = {"calls": {}, "outreach": {}, "permissions": {}, "callbacks": []}
        if self.state_path.exists():
            self.state.update(json.loads(self.state_path.read_text(encoding="utf-8")))

    # -- persistence
    def save(self) -> None:
        with _LOCK:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.state_path)

    def _append(self, path: Path, row: dict) -> None:
        with _LOCK, path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # -- events (shown live in the demo page and kept as the audit trail)
    def event(self, call_id: str | None, kind: str, **data) -> dict:
        row = {"at": utcnow(), "call_id": call_id, "kind": kind, **data}
        self._append(self.events_path, row)
        with _LOCK:
            if call_id and call_id in self.state["calls"]:
                self.state["calls"][call_id].setdefault("events", []).append(row)
                self.save()
        return row

    def decision(self, row: dict) -> None:
        """Same shape as the WhatsApp decision record (handoff section 8) plus channel=VOICE."""
        self._append(self.decisions_path, row)

    def decided_ids(self) -> set[str]:
        """Suggestions the owner already answered (any channel): never offered again."""
        if not self.decisions_path.exists():
            return set()
        out = set()
        for line in self.decisions_path.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r.get("decision") in ("ACCEPTED", "EDITED", "REJECTED"):
                out.add(r["suggestion_id"])
        return out

    # -- calls
    def new_call(self, session: dict) -> dict:
        with _LOCK:
            call_id = "call_" + uuid.uuid4().hex          # also the per-call capability token for the tools
            session = {**session, "call_id": call_id, "created_at": utcnow(), "status": "created",
                       "decisions": {}, "events": []}
            self.state["calls"][call_id] = session
            self.save()
            return session

    def call(self, call_id: str) -> dict | None:
        return self.state["calls"].get(call_id)

    def update_call(self, call_id: str, **fields) -> dict:
        with _LOCK:
            c = self.state["calls"][call_id]
            c.update(fields)
            self.save()
            return c

    def calls_for_bundle(self, bundle_id: str) -> list[dict]:
        return [c for c in self.state["calls"].values() if c.get("bundle_id") == bundle_id]

    # -- outreach history (text messages the messaging layer already sent)
    def outreach(self, bundle_id: str) -> list[dict]:
        return self.state["outreach"].get(bundle_id, [])

    def add_outreach(self, bundle_id: str, row: dict) -> None:
        with _LOCK:
            self.state["outreach"].setdefault(bundle_id, []).append(row)
            self.save()

    # -- standing permissions
    def permission(self, entity_id: str) -> dict | None:
        return self.state["permissions"].get(entity_id)

    def set_permission(self, entity_id: str, row: dict) -> None:
        with _LOCK:
            self.state["permissions"][entity_id] = row
            self.save()

    def add_callback(self, row: dict) -> None:
        with _LOCK:
            self.state["callbacks"].append(row)
            self.save()

    def reset(self) -> None:
        with _LOCK:
            self.state = {"calls": {}, "outreach": {}, "permissions": {}, "callbacks": []}
            self.save()
            for p in (self.decisions_path, self.events_path):
                if p.exists():
                    p.unlink()
