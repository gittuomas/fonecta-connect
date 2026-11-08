"""Connect Voice: the phone escalation step of "Connect Suggests, Customer Approves".

    uvicorn voice_service.app:app --port 8000        (from Solution/voice)

Routes
  Demo console       GET  /                                   phone UI + back-office log (browser mode)
  Bundles            GET  /voice/bundles                      every demo bundle with a policy preview
  Escalate           POST /voice/escalate/{key}?mode=browser|phone[&force=1]
  Call state         GET  /voice/calls/{call_id}              decisions + events (polled by the console)
  Browser session    GET  /voice/calls/{call_id}/session      agent id + dynamic variables for the web call
                     POST /voice/calls/{call_id}/started|ended
  Agent tools        POST /voice/tools/record-decision        called by the ElevenLabs agent during the call
                     POST /voice/tools/standing-permission
                     POST /voice/tools/contact-preference
  Post-call webhook  POST /webhooks/elevenlabs                transcript, analysis, call failures
  Demo helpers       POST /voice/demo/reset
"""
from __future__ import annotations

import json
import threading
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import bundles, config, elevenlabs_api, i18n, policy, yext_writer
from .hours import normalize, pretty
from .store import Store, utcnow

app = FastAPI(title="Connect Voice", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
store = Store()
NO_CACHE = {"Cache-Control": "no-store"}   # the pages change during the hackathon; never serve a stale copy
STATIC = Path(__file__).parent / "static"
DEMO_KEYS = [bundles.SANDBOX_BUNDLE] + bundles.DEMO_ENTITIES
PREVIEW_CALL_ID = "preview"


# ---------------------------------------------------------------------------- helpers
def _now() -> datetime:
    return policy.demo_now()


def _bundle(key: str) -> dict:
    if key != bundles.SANDBOX_BUNDLE and key not in bundles.entities():
        raise HTTPException(404, f"unknown business {key}")
    live = None
    if key == bundles.SANDBOX_BUNDLE:
        ent = yext_writer.get_entity(config.YEXT_SANDBOX_ENTITY_ID)
        live = lambda d: yext_writer.shown_hours(ent, d)  # noqa: E731
    b = bundles.build_bundle(key, _now(), live)
    done = store.decided_ids()
    if done:                                     # the owner already answered these: never ask twice
        b["items"] = [it for it in b["items"] if it["suggestion_id"] not in done]
        for n, it in enumerate(b["items"], 1):
            it["item"] = n
        b["headline"] = i18n.join([it["day_name"] for it in b["items"]], b["language"])
        b["greeting"] = i18n.greeting(b["language"], name=b["owner_first_name"], brand=b["brand"],
                                      business=b["business_name"], headline=b["headline"])
    return b


def _seed_outreach(bundle: dict) -> None:
    """Demo stand-in for the messaging layer: the text went out 26 h ago and nobody answered."""
    if store.outreach(bundle["bundle_id"]):
        return
    ch = bundle["preferred_channel"] if bundle["preferred_channel"] in ("WHATSAPP", "SMS") else "WHATSAPP"
    sent = (_now() - timedelta(hours=26)).isoformat()
    store.add_outreach(bundle["bundle_id"], {"channel": ch, "sent_at": sent, "replied": False, "simulated": True,
                                             "items": [it["suggestion_id"] for it in bundle["items"]]})


def _text_history(bundle: dict) -> str:
    rows = [o for o in store.outreach(bundle["bundle_id"]) if o.get("channel") != "VOICE"]
    if not rows:
        return "no earlier message"
    last = max(rows, key=lambda o: o["sent_at"])
    h = (_now() - datetime.fromisoformat(last["sent_at"])).total_seconds() / 3600
    return f"a {policy.CHANNEL_NAME.get(last['channel'], last['channel'])} message {h:.0f} hours ago, no reply yet"


def _dynamic_variables(call: dict) -> dict:
    b = call["bundle"]
    today = _now().date()
    return {
        "call_id": call["call_id"],
        "brand": b["brand"], "business_name": b["business_name"], "city": b["city"],
        "owner_first_name": b["owner_first_name"], "language_name": i18n.LANGUAGE_NAME[b["language"]],
        "today": f"{i18n.date_label(today, 'en')} {today.year}",
        "text_history": _text_history(b), "item_count": len(b["items"]),
        "suggestions": bundles.brief(b), "greeting": b["greeting"], "voicemail_message": b["voicemail_message"],
    }


def _public_call(call: dict) -> dict:
    b = call["bundle"]
    return {k: call.get(k) for k in ("call_id", "status", "mode", "created_at", "conversation_id", "decisions",
                                     "events", "policy", "ended_reason", "post_call")} | {
        "business_name": b["business_name"], "owner_first_name": b["owner_first_name"], "language": b["language"],
        "items": b["items"], "write_target": yext_writer.target_name(b["write_entity_id"]),
        "brand": b["brand"], "greeting": b["greeting"]}


def _tool_reply(ok: bool, status: str, say: str, **extra) -> JSONResponse:
    # Always 200: the agent reads `status` and `say`; an HTTP error would surface as a generic failure
    return JSONResponse({"ok": ok, "status": status, "say": say, **extra})


def _get_call_for_tool(call_id: str) -> dict | None:
    if call_id == PREVIEW_CALL_ID:
        # Dashboard preview and agent tests: use (or open) a session on the sandbox business
        live = [c for c in store.state["calls"].values()
                if c.get("preview") and c["status"] not in ("completed", "failed")]
        if live:
            return live[-1]
        b = _bundle(bundles.SANDBOX_BUNDLE)
        call = store.new_call({"bundle_id": b["bundle_id"], "bundle": b, "mode": "preview", "preview": True})
        store.update_call(call["call_id"], status="in_progress")
        store.event(call["call_id"], "preview_session_opened")
        return call
    return store.call(call_id)


def _remaining(call: dict) -> list[dict]:
    done = {int(k) for k in call["decisions"]}
    return [it for it in call["bundle"]["items"] if it["item"] not in done]


def _next_hint(call: dict) -> str:
    rest = _remaining(call)
    if not rest:
        return "All dates are decided. Offer standing permission if it fits, then close the call."
    it = rest[0]
    return f"Next: item {it['item']}, {it['day_name_en']} {it['date_label_en']}."


# ---------------------------------------------------------------------------- console + bundles
@app.get("/")
def console():
    return FileResponse(STATIC / "console.html", headers=NO_CACHE)


@app.get("/health")
def health():
    return {"ok": True, "now": _now().isoformat(), "agent_id": config.ELEVENLABS_AGENT_ID or None,
            "public_base_url": config.PUBLIC_BASE_URL, "phone_ready": bool(config.ELEVENLABS_PHONE_NUMBER_ID),
            "webhook_secret_set": bool(config.ELEVENLABS_WEBHOOK_SECRET)}


@app.get("/voice/config")
def client_config():
    return {"agent_id": config.ELEVENLABS_AGENT_ID, "now": _now().isoformat(),
            "phone_ready": bool(config.ELEVENLABS_PHONE_NUMBER_ID and config.ELEVENLABS_API_KEY)}


@app.get("/voice/bundles")
def list_bundles():
    out = []
    for key in DEMO_KEYS:
        try:
            b = _bundle(key)
        except Exception as e:  # noqa: BLE001  (sandbox offline etc. should not break the list)
            out.append({"key": key, "error": str(e)})
            continue
        _seed_outreach(b)
        pol = policy.evaluate(b, store.outreach(b["bundle_id"]), store.calls_for_bundle(b["bundle_id"]), _now(),
                              "browser")
        out.append({"key": key, "bundle_id": b["bundle_id"], "business_name": b["business_name"],
                    "city": b["city"], "owner_first_name": b["owner_first_name"], "language": b["language"],
                    "preferred_channel": b["preferred_channel"], "items": b["items"],
                    "flags": len(b["flags"]), "policy": pol,
                    "write_target": yext_writer.target_name(b["write_entity_id"])})
    return out


@app.post("/voice/escalate/{key}")
def escalate(key: str, mode: Literal["browser", "phone"] = "browser", force: bool = False):
    b = _bundle(key)
    _seed_outreach(b)
    pol = policy.evaluate(b, store.outreach(b["bundle_id"]), store.calls_for_bundle(b["bundle_id"]), _now(), mode)
    if pol["decision"] != "CALL" and not force:
        return {"placed": False, "policy": pol}
    keep = set(pol["items"])
    b = {**b, "items": [it for it in b["items"] if it["item"] in keep]}
    for n, it in enumerate(b["items"], 1):                    # renumber after dropping low-confidence items
        it["item"] = n
    call = store.new_call({"bundle_id": b["bundle_id"], "bundle": b, "mode": mode, "policy": pol,
                           "forced": force and pol["decision"] != "CALL"})
    cid = call["call_id"]
    store.event(cid, "escalated", decision=pol["decision"], forced=call["forced"],
                reasons=[c["detail"] for c in pol["checks"]])
    dv = _dynamic_variables(call)
    store.update_call(cid, dynamic_variables=dv)
    if mode == "phone":
        try:
            res = elevenlabs_api.outbound_call(b["mobile"], dv, i18n.ELEVENLABS_LANG[b["language"]])
        except elevenlabs_api.ElevenLabsError as e:
            store.update_call(cid, status="failed", ended_reason=str(e))
            store.event(cid, "call_failed", error=str(e))
            raise HTTPException(502, str(e)) from None
        store.update_call(cid, status="dialing", conversation_id=res.get("conversation_id"),
                          call_sid=res.get("callSid") or res.get("sip_call_id"))
        store.event(cid, "dialing", to=b["mobile"][:-4] + "····", conversation_id=res.get("conversation_id"))
    else:
        store.update_call(cid, status="ringing")
        threading.Thread(target=_prefetch, args=(b["write_entity_id"],), daemon=True).start()
        store.event(cid, "ringing", note="browser call: answer in the demo console")
    store.add_outreach(b["bundle_id"], {"channel": "VOICE", "sent_at": _now().isoformat(), "call_id": cid})
    return {"placed": True, "call_id": cid, "policy": pol, "mode": mode}


def _prefetch(entity_id: str) -> None:
    try:
        yext_writer.prefetch(entity_id)
    except yext_writer.YextError:
        pass


@app.get("/voice/ringing")
def ringing():
    calls = [c for c in store.state["calls"].values() if c["status"] == "ringing"]
    return _public_call(calls[-1]) if calls else {}


@app.get("/voice/calls/{call_id}")
def get_call(call_id: str):
    c = store.call(call_id)
    if not c:
        raise HTTPException(404, "unknown call")
    return _public_call(c)


@app.get("/voice/calls/{call_id}/session")
def call_session(call_id: str):
    c = store.call(call_id)
    if not c:
        raise HTTPException(404, "unknown call")
    lang = c["bundle"]["language"]
    out = {"agent_id": config.ELEVENLABS_AGENT_ID, "dynamic_variables": c["dynamic_variables"],
           "language": i18n.ELEVENLABS_LANG[lang]}
    if config.ELEVENLABS_API_KEY:
        try:
            out["conversation_token"] = elevenlabs_api.conversation_token()
        except elevenlabs_api.ElevenLabsError:
            pass
    return out


class StartedIn(BaseModel):
    conversation_id: Optional[str] = None


@app.post("/voice/calls/{call_id}/started")
def call_started(call_id: str, body: StartedIn):
    c = store.call(call_id) or HTTPException(404)
    store.update_call(call_id, status="in_progress", conversation_id=body.conversation_id)
    store.event(call_id, "answered", conversation_id=body.conversation_id)
    return {"ok": True}


@app.post("/voice/calls/{call_id}/ended")
def call_ended(call_id: str, body: StartedIn):
    c = store.call(call_id)
    if not c:
        raise HTTPException(404, "unknown call")
    _finalize(c, reason="hung up (browser)")
    return _public_call(store.call(call_id))


def _finalize(call: dict, reason: str) -> None:
    """Close the call: every undecided item stays open and goes back to the text channel."""
    if call["status"] in ("completed", "failed"):
        return
    for it in _remaining(call):
        store.decision({"suggestion_id": it["suggestion_id"], "decision": "NO_RESPONSE", "final_hours": None,
                        "decided_at": utcnow(), "channel": "VOICE", "standing_permission_granted": False,
                        "owner_comment": None, "call_id": call["call_id"],
                        "conversation_id": call.get("conversation_id")})
    decided = [d for d in call["decisions"].values() if d["decision"] in ("ACCEPTED", "EDITED")
               and d.get("publish_state") != "failed"]
    failed = [d for d in call["decisions"].values() if d.get("publish_state") == "failed"]
    receipt = "; ".join(f"{d['day_name_en']} {d['date']}: {pretty(d['final_hours'])}" for d in decided)
    store.update_call(call["call_id"], status="completed", ended_reason=reason, ended_at=utcnow())
    store.event(call["call_id"], "call_ended", reason=reason, open_items=[it["item"] for it in _remaining(call)])
    if decided:
        store.event(call["call_id"], "receipt_queued",
                    channel=call["bundle"]["preferred_channel"],
                    text=f"As agreed on the phone, these hours are now updated: {receipt}. Reply UNDO to revert.")
    if failed:
        store.event(call["call_id"], "publish_followup", items=[d["date"] for d in failed],
                    note="saved on the call but the listing write failed; the team publishes it and confirms by message")


# ---------------------------------------------------------------------------- agent tools
class DecisionIn(BaseModel):
    call_id: str
    item: int = Field(..., ge=1)
    decision: str
    hours: Optional[str] = None
    confirmed: bool = False
    owner_comment: Optional[str] = None
    conversation_id: Optional[str] = None


DECISION_ALIASES = {"APPROVE": "APPROVE", "ACCEPT": "APPROVE", "ACCEPTED": "APPROVE", "YES": "APPROVE",
                    "EDIT": "EDIT", "EDITED": "EDIT", "CHANGE": "EDIT",
                    "DECLINE": "DECLINE", "REJECT": "DECLINE", "REJECTED": "DECLINE", "NO": "DECLINE",
                    "KEEP": "DECLINE"}


@app.post("/voice/tools/record-decision")
def record_decision(body: DecisionIn):
    call = _get_call_for_tool(body.call_id)
    if not call:
        return _tool_reply(False, "UNKNOWN_CALL", "This call is not linked to any suggestions. Do not save "
                           "anything; tell the owner a colleague will follow up by message.")
    cid = call["call_id"]
    if body.conversation_id and not call.get("conversation_id"):
        store.update_call(cid, conversation_id=body.conversation_id)
    item = next((it for it in call["bundle"]["items"] if it["item"] == body.item), None)
    store.event(cid, "tool_call", tool="record_decision",
                args=body.model_dump(exclude={"call_id", "conversation_id"}))
    if not item:
        return _tool_reply(False, "UNKNOWN_ITEM", f"There is no item {body.item}. Items are 1 to "
                           f"{len(call['bundle']['items'])}.")
    if str(body.item) in call["decisions"]:
        prev = call["decisions"][str(body.item)]
        return _tool_reply(True, "ALREADY_DECIDED", f"Item {body.item} was already saved as {prev['decision']}.",
                           **{"next": _next_hint(call)})
    kind = DECISION_ALIASES.get(body.decision.strip().upper())
    if not kind:
        return _tool_reply(False, "INVALID_DECISION", "decision must be APPROVE, EDIT or DECLINE.")
    if not body.confirmed:
        return _tool_reply(False, "NEEDS_CONFIRMATION", "Nothing saved. Read the final hours back to the owner "
                           "and ask for an explicit yes, then call again with confirmed=true.")

    final, applied, target = None, False, None
    if kind == "APPROVE":
        final = item["proposed_hours"]
    elif kind == "EDIT":
        try:
            final = normalize(body.hours)
        except ValueError as e:
            store.event(cid, "invalid_hours", raw=body.hours, error=str(e))
            return _tool_reply(False, "INVALID_HOURS", f"Nothing saved. {e}")
        if final == item["proposed_hours"]:
            kind = "APPROVE"
        elif final == item["shown_hours"]:
            kind = "DECLINE"          # the owner says what is shown now is right: nothing to write

    outcome = {"APPROVE": "ACCEPTED", "EDIT": "EDITED", "DECLINE": "REJECTED"}[kind]
    d = date.fromisoformat(item["date"])
    write_eid = call["bundle"]["write_entity_id"]
    if outcome in ("ACCEPTED", "EDITED"):
        try:
            target = yext_writer.target_name(write_eid)
            yext_writer._target(write_eid)        # fail fast here if the sandbox key is missing
            applied = True                        # accepted for publishing; the write runs in the background
        except yext_writer.YextError as e:
            store.event(cid, "yext_write_failed", date=item["date"], hours=final, error=str(e))

    rec = {"suggestion_id": item["suggestion_id"], "decision": outcome,
           "final_hours": final if outcome != "REJECTED" else None, "decided_at": utcnow(), "channel": "VOICE",
           "standing_permission_granted": False, "owner_comment": body.owner_comment,
           "call_id": cid, "conversation_id": call.get("conversation_id"), "entity_id": call["bundle"]["entity_id"],
           "date": item["date"], "day_name_en": item["day_name_en"], "shown_hours": item["shown_hours"],
           "proposed_hours": item["proposed_hours"], "applied": applied, "yext_target": target,
           "publish_state": "publishing" if applied else None}
    store.decision(rec)
    call = store.call(cid)
    call["decisions"][str(body.item)] = rec
    store.save()
    store.event(cid, "decision", item=body.item, decision=outcome, final_hours=rec["final_hours"],
                applied=applied)

    if applied:
        def _verified(ok, got, _cid=cid, _date=item["date"], _final=final):
            store.event(_cid, "yext_verified" if ok else "yext_verify_failed", date=_date, hours=_final,
                        **({} if ok else {"got": got}))

        def _done(ok, res, _cid=cid, _item=str(body.item), _date=item["date"], _final=final):
            if ok:
                store.event(_cid, "yext_write", target=res["target"], date=_date, hours=_final, written=res["written"])
            else:
                store.event(_cid, "yext_write_failed", date=_date, hours=_final, error=res)
            c = store.call(_cid)
            r = (c or {}).get("decisions", {}).get(_item)
            if r:
                r["applied"], r["publish_state"] = ok, ("live" if ok else "failed")
                store.save()

        yext_writer.write_async(write_eid, d, final, on_done=_done, on_verified=_verified)

    if outcome == "REJECTED":
        say = "Kept as is."
    elif applied:
        say = "Done."
    else:
        say = ("The decision is saved, but publishing failed for a technical reason. Tell the owner our team "
               "will publish it and they will get a confirmation message. Do not retry.")
    return _tool_reply(True, "APPLIED" if applied else ("DECLINED" if outcome == "REJECTED" else "SAVED_NOT_PUBLISHED"),
                       say, item=body.item, final_hours=rec["final_hours"],
                       live_on="Google within minutes, other sites within a few days" if applied else None,
                       next=_next_hint(call),
                       remaining_items=[it["item"] for it in _remaining(call)])


class PermissionIn(BaseModel):
    call_id: str
    scope: str = "PUBLIC_HOLIDAYS"
    granted: bool = True
    confirmed: bool = False


@app.post("/voice/tools/standing-permission")
def standing_permission(body: PermissionIn):
    call = _get_call_for_tool(body.call_id)
    if not call:
        return _tool_reply(False, "UNKNOWN_CALL", "This call is not linked to a business. Do not promise anything.")
    store.event(call["call_id"], "tool_call", tool="set_standing_permission", args=body.model_dump(exclude={"call_id"}))
    if not body.confirmed:
        return _tool_reply(False, "NEEDS_CONFIRMATION", "Nothing saved. Ask the owner for an explicit yes first.")
    eid = call["bundle"]["entity_id"]
    store.set_permission(eid, {"scope": body.scope, "granted": body.granted, "at": utcnow(),
                               "channel": "VOICE", "call_id": call["call_id"]})
    for rec in call["decisions"].values():
        rec["standing_permission_granted"] = body.granted
    store.save()
    store.event(call["call_id"], "standing_permission", scope=body.scope, granted=body.granted)
    if body.granted:
        say = ("Saved. From now on we handle public holidays automatically using their own past choices, "
               "send a message every time, and they can undo or switch this off by replying STOP.")
    else:
        say = "Saved. We keep asking before every change."
    return _tool_reply(True, "SAVED", say)


class PreferenceIn(BaseModel):
    call_id: str
    preference: str               # CALL_LATER | TEXT_ONLY | NO_MORE_CALLS
    when: Optional[str] = None
    note: Optional[str] = None


@app.post("/voice/tools/contact-preference")
def contact_preference(body: PreferenceIn):
    call = _get_call_for_tool(body.call_id)
    if not call:
        return _tool_reply(False, "UNKNOWN_CALL", "Not linked to a business.")
    store.event(call["call_id"], "tool_call", tool="set_contact_preference", args=body.model_dump(exclude={"call_id"}))
    pref = body.preference.strip().upper()
    store.add_callback({"entity_id": call["bundle"]["entity_id"], "preference": pref, "when": body.when,
                        "note": body.note, "at": utcnow(), "call_id": call["call_id"]})
    say = {"CALL_LATER": f"Saved. We will call back {body.when or 'later'}.",
           "TEXT_ONLY": "Saved. We will only use messages from now on.",
           "NO_MORE_CALLS": "Saved. We will not call again."}.get(pref, "Saved.")
    return _tool_reply(True, "SAVED", say)


# ---------------------------------------------------------------------------- post-call webhook
@app.post("/webhooks/elevenlabs")
async def elevenlabs_webhook(request: Request, elevenlabs_signature: Optional[str] = Header(None)):
    raw = await request.body()
    verified = elevenlabs_api.verify_webhook(raw, elevenlabs_signature)
    if config.ELEVENLABS_WEBHOOK_SECRET and not verified:
        raise HTTPException(401, "bad signature")
    payload = json.loads(raw or b"{}")
    kind = payload.get("type")
    data = payload.get("data") or {}
    dv = ((data.get("conversation_initiation_client_data") or {}).get("dynamic_variables") or {})
    call = store.call(dv.get("call_id", "")) if dv.get("call_id") else None
    if call is None and data.get("conversation_id"):
        call = next((c for c in store.state["calls"].values()
                     if c.get("conversation_id") == data["conversation_id"]), None)
    # Keep the raw payload as the audit record of the conversation
    folder = config.DATA_DIR / "conversations"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{data.get('conversation_id', 'unknown')}_{kind}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    if not call:
        return {"ok": True, "matched": False}
    cid = call["call_id"]
    if kind == "post_call_transcription":
        analysis = data.get("analysis") or {}
        summary = {"verified": verified, "summary": analysis.get("transcript_summary"),
                   "call_successful": analysis.get("call_successful"),
                   "data_collection": {k: v.get("value") for k, v in (analysis.get("data_collection_results") or {}).items()},
                   "evaluation": {k: v.get("result") for k, v in (analysis.get("evaluation_criteria_results") or {}).items()},
                   "duration_s": (data.get("metadata") or {}).get("call_duration_secs")}
        store.update_call(cid, post_call=summary, conversation_id=data.get("conversation_id"))
        store.event(cid, "post_call_analysis", **summary)
        _finalize(store.call(cid), reason="call ended (post-call webhook)")
    elif kind == "call_initiation_failure":
        reason = (data.get("failure_reason") or "no answer")
        store.update_call(cid, status="failed", ended_reason=reason)
        store.event(cid, "call_not_connected", reason=reason,
                    fallback="suggestion stays open in the text channel; a reminder goes out")
    return {"ok": True, "matched": True}


# ---------------------------------------------------------------------------- demo helpers
@app.post("/voice/demo/reset")
def demo_reset(sandbox: bool = True):
    """Forget calls and decisions; optionally remove the holiday hours the demo wrote to the sandbox."""
    removed = []
    if sandbox:
        written = {e["date"] for c in store.state["calls"].values() for e in c.get("events", [])
                   if e["kind"] == "yext_write" and e.get("target") == "Yext sandbox"}
        if written:
            ent = yext_writer.get_entity(config.YEXT_SANDBOX_ENTITY_ID)
            hours = ent.get("hours") or {}
            hours["holidayHours"] = [h for h in hours.get("holidayHours", []) if h.get("date") not in written]
            yext_writer._req(config.YEXT_SANDBOX_ENTITY_ID, "PUT", {"hours": hours})
            removed = sorted(written)
    store.reset()
    yext_writer.forget()
    mock = False
    try:                                         # undo the demo's writes in the package's Yext mock too
        req = urllib.request.Request(config.YEXT_MOCK_BASE + "/mock/reset", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
        mock = True
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "sandbox_dates_removed": removed, "mock_reset": mock}

# ---------------------------------------------------------------------------- benchmark (eval.html)
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "agent" / "prompts"
EVAL_LOG = config.DATA_DIR / "eval_results.jsonl"


@app.get("/eval")
def eval_page():
    return FileResponse(STATIC / "eval.html", headers=NO_CACHE)


@app.get("/eval/prompts")
def eval_prompts():
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(PROMPTS_DIR.glob("*.md"))}


@app.post("/eval/results")
async def eval_save(request: Request):
    row = await request.json()
    row["saved_at"] = utcnow()
    EVAL_LOG.parent.mkdir(parents=True, exist_ok=True)
    with EVAL_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {"ok": True}


@app.get("/eval/results")
def eval_results():
    if not EVAL_LOG.exists():
        return []
    return [json.loads(l) for l in EVAL_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
