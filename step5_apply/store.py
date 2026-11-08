"""Local state for step 5: audit log (for undo + accountability), standing permissions (learning), CRM contacts."""
from __future__ import annotations

import csv
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import hours as H
from .config import DATA


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class AuditLog:
    """Append-only JSONL. Every write to Yext gets one record with before/after per date, so it can be undone."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, record: dict) -> str:
        record = {"id": record.get("id") or uuid.uuid4().hex[:12], "at": now_iso(), **record}
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record["id"]

    def all(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self._lock, open(self.path, encoding="utf-8") as f:   # same lock as append: never read half a line
            return [json.loads(line) for line in f if line.strip()]

    def get(self, record_id: str, records: list[dict] | None = None) -> dict | None:
        return next((r for r in (self.all() if records is None else records) if r["id"] == record_id), None)

    def undone_dates(self, record_id: str, records: list[dict] | None = None) -> set[str]:
        """Dates of an APPLY that a later UNDO has put back. An undo can be partial (one date failed or conflicted)."""
        return {i["date"] for r in (self.all() if records is None else records) if r.get("undo_of") == record_id
                for i in r.get("items", [])}

    def undone(self, record_id: str, records: list[dict] | None = None) -> bool:
        """True only when EVERY date of the APPLY has been undone, so a partly failed undo can be retried."""
        records = self.all() if records is None else records
        rec = self.get(record_id, records)
        if not rec:
            return False
        return {i["date"] for i in rec.get("items", [])} <= self.undone_dates(record_id, records)


class StandingPermissions:
    """'Closed every Christmas Eve, don't ask again.' Keyed by (entity, day_key) -> hours the owner approved."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8") or "{}")

    def _save(self, data: dict) -> None:
        """Write to a temp file and swap it in, so a reader never sees a half-written file."""
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def grant(self, entity_id: str, day_key: str, hours: str, source: str = "owner") -> None:
        with self._lock:
            data = self._load()
            data.setdefault(entity_id, {})[day_key] = {"hours": H.normalise(hours), "granted_at": now_iso(),
                                                       "source": source}
            self._save(data)

    def revoke(self, entity_id: str, day_key: str) -> None:
        with self._lock:
            data = self._load()
            data.get(entity_id, {}).pop(day_key, None)
            self._save(data)

    def lookup(self, entity_id: str, day_key: str) -> dict | None:
        with self._lock:
            return self._load().get(entity_id, {}).get(day_key)

    def covers(self, entity_id: str, day_key: str, hours: str) -> bool:
        p = self.lookup(entity_id, day_key)
        return bool(p) and p["hours"] == H.normalise(hours)


def load_contacts(path: Path = DATA / "customer_contacts.csv") -> dict[str, dict]:
    """CRM rows by entity_id: preferred channel/language, standing_permission_holidays, opted_out_of_suggestions."""
    with open(path, encoding="utf-8") as f:
        return {r["entity_id"]: r for r in csv.DictReader(f)}
