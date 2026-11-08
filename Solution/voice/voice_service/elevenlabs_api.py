"""Thin ElevenLabs Agents API client (standard library only).

Needs ELEVENLABS_API_KEY for everything except webhook verification. Browser-mode demos with a
public agent need no key at all: the page starts the session with the agent id.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config


class ElevenLabsError(RuntimeError):
    pass


def _req(method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
    if not config.ELEVENLABS_API_KEY:
        raise ElevenLabsError("ELEVENLABS_API_KEY is not set in .env")
    url = config.ELEVENLABS_API_BASE + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "xi-api-key": config.ELEVENLABS_API_KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise ElevenLabsError(f"{method} {path} -> {e.code}: {e.read().decode()[:500]}") from None


def initiation_data(dynamic_variables: dict, language: str | None) -> dict:
    data = {"dynamic_variables": dynamic_variables}
    if language and language != "en":
        data["conversation_config_override"] = {"agent": {"language": language}}
    return data


def outbound_call(to_number: str, dynamic_variables: dict, language: str | None) -> dict:
    """Place one outbound phone call through the imported Twilio number (or SIP trunk)."""
    if not (config.ELEVENLABS_AGENT_ID and config.ELEVENLABS_PHONE_NUMBER_ID):
        raise ElevenLabsError("ELEVENLABS_AGENT_ID and ELEVENLABS_PHONE_NUMBER_ID must be set for phone calls")
    path = "/v1/convai/sip-trunk/outbound-call" if config.ELEVENLABS_TELEPHONY == "sip_trunk" \
        else "/v1/convai/twilio/outbound-call"
    return _req("POST", path, {
        "agent_id": config.ELEVENLABS_AGENT_ID,
        "agent_phone_number_id": config.ELEVENLABS_PHONE_NUMBER_ID,
        "to_number": to_number,
        "conversation_initiation_client_data": initiation_data(dynamic_variables, language),
    })


def batch_call(name: str, recipients: list[dict], scheduled_unix: int | None = None) -> dict:
    """Morning sweep: one batch for every owner the policy scheduled into the same window.

    recipients: [{"phone_number": "+358...", "dynamic_variables": {...}, "language": "fi"}]
    """
    body = {
        "call_name": name, "agent_id": config.ELEVENLABS_AGENT_ID,
        "agent_phone_number_id": config.ELEVENLABS_PHONE_NUMBER_ID,
        "recipients": [{"phone_number": r["phone_number"],
                        "conversation_initiation_client_data": initiation_data(r["dynamic_variables"],
                                                                               r.get("language"))}
                       for r in recipients],
    }
    if scheduled_unix:
        body["scheduled_time_unix"] = scheduled_unix
    return _req("POST", "/v1/convai/batch-calling/submit", body)


def conversation_token(agent_id: str | None = None) -> str:
    """WebRTC token for a private agent (browser mode with authentication on)."""
    r = _req("GET", "/v1/convai/conversation/token", params={"agent_id": agent_id or config.ELEVENLABS_AGENT_ID})
    return r["token"]


def get_conversation(conversation_id: str) -> dict:
    return _req("GET", f"/v1/convai/conversations/{conversation_id}")


def verify_webhook(raw_body: bytes, signature_header: str | None, tolerance_s: int = 1800) -> bool:
    """ElevenLabs-Signature: t=<unix>,v0=<hex hmac_sha256(secret, f"{t}.{body}")>."""
    if not config.ELEVENLABS_WEBHOOK_SECRET:
        return False
    if not signature_header:
        return False
    parts = dict(p.split("=", 1) for p in signature_header.split(",") if "=" in p)
    ts, sig = parts.get("t"), parts.get("v0")
    if not ts or not sig or abs(time.time() - int(ts)) > tolerance_s:
        return False
    mac = hmac.new(config.ELEVENLABS_WEBHOOK_SECRET.encode(), f"{ts}.".encode() + raw_body, hashlib.sha256)
    return hmac.compare_digest(mac.hexdigest(), sig)
