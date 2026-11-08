"""End-to-end test of the voice escalation backend without ElevenLabs.

Starts the package's Yext mock on a free port, then plays the part of the agent:
escalate -> answer -> approve / edit / decline -> standing permission -> hang up -> webhook.

    cd Solution/voice && python -m pytest tests -q      (or: python tests/test_flow.py)
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def wait_for(cond, timeout=8.0, step=0.1) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _start_mock(pkg: Path) -> str:
    sys.path.insert(0, str(pkg / "mock_api"))
    os.environ.setdefault("MOCK_QUIET", "1")
    import server  # package mock, used unmodified
    server.STATE_FILE = os.path.join(tempfile.mkdtemp(), "state.json")
    server.STORE = server.Store()
    port = _free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    return f"http://127.0.0.1:{port}"


def test_full_call():
    os.environ["VOICE_DATA_DIR"] = tempfile.mkdtemp()
    os.environ.setdefault("YEXT_API_KEY", "")
    from voice_service import config
    os.environ["YEXT_MOCK_BASE"] = _start_mock(config.PKG_DIR)
    import importlib
    importlib.reload(config)
    from voice_service import yext_writer, store as store_mod, app as app_mod
    importlib.reload(yext_writer)
    importlib.reload(store_mod)
    importlib.reload(app_mod)
    from fastapi.testclient import TestClient
    c = TestClient(app_mod.app)

    # 1. Policy: Kahvila Aurora on the demo clock (15 Dec) should be called
    r = c.post("/voice/escalate/FI-D0001?mode=browser").json()
    assert r["placed"], r["policy"]
    cid = r["call_id"]
    call = c.get(f"/voice/calls/{cid}").json()
    items = call["items"]
    assert [i["day_key"] for i in items] == ["XMAS_EVE", "XMAS_DAY", "NEW_YEARS_DAY"], items
    s = c.get(f"/voice/calls/{cid}/session").json()
    assert s["dynamic_variables"]["call_id"] == cid and "Christmas Eve" in s["dynamic_variables"]["suggestions"]
    print("\nGREETING:", s["dynamic_variables"]["greeting"])
    print("SUGGESTIONS:\n" + s["dynamic_variables"]["suggestions"])

    # 2. Pizzeria Torni (closed for good) must never be called; Otso has standing permission
    assert c.post("/voice/escalate/FI-D0011").json()["placed"] is False
    assert c.post("/voice/escalate/FI-D0012").json()["placed"] is False

    tool = lambda **kw: c.post("/voice/tools/record-decision", json={"call_id": cid, **kw}).json()  # noqa: E731
    c.post(f"/voice/calls/{cid}/started", json={"conversation_id": "conv_test"})

    # 3. Guardrail: no confirmation, nothing saved
    r = tool(item=1, decision="APPROVE", confirmed=False)
    assert r["status"] == "NEEDS_CONFIRMATION"
    # 4. Approve item 1, verify the write landed in the (mock) Yext entity
    r = tool(item=1, decision="APPROVE", confirmed=True)
    assert r["status"] == "APPLIED", r
    print("TOOL SAY:", r["say"])
    # The write runs in the background (the owner is not held for the Yext round trip): wait for it
    def landed():
        hol = yext_writer.get_entity("FI-D0001")["hours"].get("holidayHours", [])
        return any(h["date"] == "2026-12-24" and (h.get("openIntervals") or [{}])[0].get("end") == "13:00" for h in hol)
    assert wait_for(landed), yext_writer.get_entity("FI-D0001")["hours"].get("holidayHours")
    # 5. Bad hours are rejected, then an edit with sloppy formatting is normalised
    assert tool(item=2, decision="EDIT", hours="sometime in the morning", confirmed=True)["status"] == "INVALID_HOURS"
    r = tool(item=2, decision="EDIT", hours="10–14", confirmed=True)
    assert r["status"] == "APPLIED" and r["final_hours"] == "10:00-14:00", r
    # 6. Decline item 3, then a duplicate call is idempotent
    assert tool(item=3, decision="DECLINE", confirmed=True)["status"] == "DECLINED"
    assert tool(item=3, decision="APPROVE", confirmed=True)["status"] == "ALREADY_DECIDED"
    # 7. Standing permission and hang-up
    r = c.post("/voice/tools/standing-permission", json={"call_id": cid, "scope": "PUBLIC_HOLIDAYS",
                                                          "granted": True, "confirmed": True}).json()
    assert r["status"] == "SAVED"
    assert wait_for(lambda: [e["kind"] for e in c.get(f"/voice/calls/{cid}").json()["events"]].count("yext_write") == 2)
    c.post(f"/voice/calls/{cid}/ended", json={})
    call = c.get(f"/voice/calls/{cid}").json()
    assert call["status"] == "completed"
    kinds = [e["kind"] for e in call["events"]]
    assert "receipt_queued" in kinds and kinds.count("yext_write") == 2, kinds
    # 8. Post-call webhook (unsigned, no secret configured) attaches the analysis
    payload = {"type": "post_call_transcription", "data": {
        "conversation_id": "conv_test", "conversation_initiation_client_data": {"dynamic_variables": {"call_id": cid}},
        "analysis": {"transcript_summary": "Owner approved two dates.", "call_successful": "success",
                     "data_collection_results": {}, "evaluation_criteria_results": {
                         "explicit_confirmation": {"result": "success"}}},
        "metadata": {"call_duration_secs": 94}}}
    assert c.post("/webhooks/elevenlabs", content=json.dumps(payload)).json()["matched"]
    decisions = [json.loads(l) for l in (config.DATA_DIR / "decisions.jsonl").read_text().splitlines()]
    assert [d["decision"] for d in decisions] == ["ACCEPTED", "EDITED", "REJECTED"], decisions
    print("DECISIONS:", [(d["suggestion_id"], d["decision"], d["final_hours"]) for d in decisions])
    print("OK")


if __name__ == "__main__":
    test_full_call()
