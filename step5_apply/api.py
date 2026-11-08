"""Step 5 as a plain Python API: JSON-style dicts in, JSON-style dicts out.

    from step5_apply.api import Step5
    s5 = Step5.from_env()                     # starts the Yext mock in-process unless CONNECT_MOCK_URL is set
    res = s5.apply({"entity_id": "FI-D0001", "suggestion_id": "msg-42",
                    "items": [{"date": "2026-12-24", "hours": "closed", "decision": "ACCEPTED", "seen_before": None}]})
    res["message"]                            # send this to the owner (None = send nothing)
    s5.handle_reply("FI-D0001", "PERU")       # owner replied UNDO -> undo the latest change

The HTTP service (service.py) exposes exactly these calls for non-Python teammates. Contract: INTEGRATION.md.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from . import config
from . import confirm as C
from .apply import Applier, Decision
from .mock_runtime import MockServer, start_mock
from .store import AuditLog, StandingPermissions, load_contacts
from .yext import MappedYextClient, YextClient, YextError


class Step5:
    def __init__(self, applier: Applier, mock: MockServer | None = None):
        self.applier, self.mock = applier, mock
        self.selection_reason = "explicit mode"

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_env(cls, mock_url: str | None = None, use_sandbox: bool | None = None,
                 runtime_dir: str | Path | None = None, today: date | None = None,
                 mode: str | None = None) -> "Step5":
        """mode='sandbox': mapped real primary; missing configuration fails closed.

        mode='auto' (default): prefer sandbox after a read-only connectivity check,
        otherwise start the offline demo. Never fall back after a write attempt.
        mode='demo': explicit challenge simulator, optionally mirrored
        to sandbox when use_sandbox or credentials enable it. Runtime state for
        sandbox mode is separate from demo state. Never expose the local demo
        mutation endpoints on a public network.
        """
        config.load_dotenv()
        mode = mode or os.environ.get("CONNECT_APPLY_MODE", "auto")
        if mode not in ("auto", "demo", "sandbox"):
            raise ValueError("CONNECT_APPLY_MODE must be auto, demo or sandbox")
        if mode == "auto":
            reason = "sandbox configuration unavailable"
            if use_sandbox is not False and not mock_url and config.sandbox_key() and config.sandbox_mirror():
                real = None
                try:
                    root = Path(runtime_dir or os.environ.get("CONNECT_RUNTIME_DIR") or config.RUNTIME)
                    real = cls.from_env(runtime_dir=root / "sandbox", today=today, mode="sandbox")
                    client = real.applier.primary.client
                    timeout, retries = client.timeout, client.max_retries
                    try:
                        client.timeout, client.max_retries = 3, 0
                        for eid in real.applier.primary.mapping:
                            real.applier.primary.get_entity(eid)
                    finally:
                        client.timeout, client.max_retries = timeout, retries
                    real.selection_reason = "sandbox read-only startup check passed"
                    return real
                except (ValueError, RuntimeError, YextError, OSError, KeyError):
                    if real:
                        real.close()
                    reason = "sandbox startup check unavailable; using offline demo"
            demo = cls.from_env(mock_url=mock_url, use_sandbox=False, runtime_dir=runtime_dir,
                                today=today, mode="demo")
            demo.selection_reason = reason
            return demo
        rt = Path(runtime_dir or os.environ.get("CONNECT_RUNTIME_DIR") or config.RUNTIME)
        if mode == "sandbox":
            if mock_url or use_sandbox is False:
                raise ValueError("Sandbox mode cannot use mock_url or use_sandbox=False")
            if config.sandbox_url().rstrip("/") != config.SANDBOX_URL:
                raise ValueError("Challenge sandbox mode requires https://sbx-api.yextapis.com")
            if not config.sandbox_key():
                raise RuntimeError("Sandbox mode requires YEXT_API_KEY; no mock fallback")
            primary = MappedYextClient(
                YextClient(config.SANDBOX_URL, config.sandbox_key(), name="sandbox"),
                config.sandbox_mirror())
            if runtime_dir is None:
                rt = rt / "sandbox"
            rt.mkdir(parents=True, exist_ok=True)
            return cls(Applier(primary, AuditLog(rt / "audit.jsonl"),
                               StandingPermissions(rt / "standing_permissions.json"), load_contacts(),
                               today=today, default_language=os.environ.get("STEP5_LANGUAGE", "en")))
        rt.mkdir(parents=True, exist_ok=True)
        mock, url = None, mock_url or os.environ.get("CONNECT_MOCK_URL")
        if not url:
            mock = start_mock(str(rt))
            url = mock.url
        primary = YextClient(url, os.environ.get("CONNECT_MOCK_KEY", "demo"), name="mock")
        if use_sandbox is None:
            use_sandbox = bool(config.sandbox_key() and config.sandbox_mirror())
        sandbox, mirror = None, {}
        if use_sandbox:
            if not config.sandbox_key():
                raise RuntimeError("sandbox requested but YEXT_API_KEY is not set (.env)")
            sandbox = YextClient(config.sandbox_url(), config.sandbox_key(), name="sandbox")
            mirror = config.sandbox_mirror()
        applier = Applier(primary, AuditLog(rt / "audit.jsonl"), StandingPermissions(rt / "standing_permissions.json"),
                          load_contacts(), sandbox=sandbox, mirror=mirror, today=today,
                          default_language=os.environ.get("STEP5_LANGUAGE", "en"))
        return cls(applier, mock)

    def close(self) -> None:
        if self.mock:
            self.mock.stop()

    # ------------------------------------------------------------------ calls
    def apply(self, decision: dict) -> dict:
        """Raises ValueError for a malformed decision; everything else comes back as item statuses."""
        return self._scoped_result(self.applier.apply(Decision.from_dict(decision)).to_dict())

    def _scoped_result(self, result: dict) -> dict:
        """A successful write establishes only the named target, not publication."""
        target = "sandbox" if self.applier.primary.name == "sandbox" else "demo"
        result["apply_target"] = target
        notice = ("Yext sandbox only. Public listings have not been verified." if target == "sandbox"
                  else "Local simulator only. Public listings have not been updated by this write.")
        mirror = result.get("mirror")
        if mirror is not None:
            notice += (" Sandbox mirror verified." if mirror.get("ok")
                       else " Sandbox mirror failed; a real sandbox update is not confirmed.")
        result["publication_verified"] = False
        # Browser demos show normal product copy; operator/API consumers retain scope.
        result["owner_message"] = result.get("message")
        result["owner_message_short"] = result.get("message_short")
        for key in ("message", "message_short"):
            if result.get(key):
                result[key] += "\n" + notice
        return result

    def undo(self, audit_id: str | None = None, entity_id: str | None = None, language: str | None = None) -> dict:
        """Undo one APPLY by audit id, or the latest one for a business."""
        if not audit_id:
            if not entity_id:
                raise ValueError("give 'audit_id' or 'entity_id'")
            audit_id = self.applier.latest_apply(entity_id)
            if not audit_id:
                return {"entity_id": entity_id, "items": [], "audit_id": None, "message": None, "mirror": None,
                        "message_short": None, "applied": [], "reason": "nothing to undo"}
        return self._scoped_result(self.applier.undo(audit_id, language).to_dict())

    def handle_reply(self, entity_id: str, text: str, language: str | None = None) -> dict | None:
        """Owner's free-text reply after a confirmation. Undo words (PERU / ÅNGRA / ANGRE / UNDO) undo the latest
        change; anything else returns None so step 4 can handle it."""
        if not C.is_undo_reply(text):
            return None
        return self.undo(entity_id=entity_id, language=language)

    def snapshot(self, entity_id: str, dates: list[str]) -> dict:
        return self.applier.snapshot(entity_id, dates)

    def policy(self, entity_id: str, day_key: str = "", hours: str | None = None, date_: str = "") -> dict:
        return self.applier.policy(entity_id, day_key, hours, date_)

    def where_it_shows(self, entity_id: str) -> list[dict]:
        return C.live_publishers(entity_id)

    def audit(self, entity_id: str | None = None) -> list[dict]:
        return [r for r in self.applier.audit.all() if entity_id is None or r["entity_id"] == entity_id]
