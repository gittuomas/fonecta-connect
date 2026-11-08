# Service configuration

The local browser simulation and the connected demo have different requirements.
Running the simulation does not verify any provider account, live source, AI model,
voice agent or real listing update. This reference records the settings actually
consumed by the code, without recording credentials.

## Local preview and prediction artifacts

Python 3.12+ and the supplied challenge files are sufficient for the local browser
simulation. It reads exported predictions and uses the local Yext simulator when
no real target is available. It does not contact OpenShift or ElevenLabs simply
because the browser page loads. Regenerating the integrated C13 artifacts needs
its pinned Python dependencies, but no inference-service API credential; see the
[engine instructions](../engine/README.md).

## Ticketmaster and live events/weather

- `TICKETMASTER_API_KEY`: Ticketmaster Consumer Key, required for its event adapter.
  The Consumer Secret is not read.
- With no key, that adapter reports `not_configured` and skips Ticketmaster. Public
  city feeds and the implemented Open-Meteo weather endpoint remain available over
  the network; they do not read an API key. This is reduced source coverage, not a
  successful Ticketmaster retrieval.
- `CONNECT_LIVE_CHECKS` selects the generated checks file in the integrated owner
  app. It is configuration, not a credential.

In the integrated component checkout, install `live_signals/requirements.txt` and
run `python -m live_signals.demo --sandbox --prepare-only` to refresh/select data
and check sandbox read access without starting the app or writing hours. Omit
`--prepare-only` to launch the owner app. `--sandbox` needs `YEXT_API_KEY`, a valid
mapping and a fresh eligible check; it does not silently replace a requested
sandbox with a mock. The default port is 8000; `--port` can change it.

Source: [provider missing-key behavior](../live_signals/providers.py),
[launcher/preflight](../live_signals/demo.py).

## OpenShift AI typed-hours interpretation

The exact settings are `OPENSHIFT_AI_URL`, `OPENSHIFT_AI_TOKEN`, and
`OPENSHIFT_AI_MODEL` (default `granite-hours`). The URL identifies the deployed
inference service; the token is sent as a Bearer credential. This is an inference
access token, not a requirement to provide a general OpenShift administrator key.

Both URL and token must be present for successful AI inference. This checkout
includes the backend adapter in `app/hours_ai.py`; the browser and real
WhatsApp worker share it. With neither setting configured, a clearly labelled
preview accepts exact entries such as `24 Dec 10-14`. Partial configuration or an
unavailable/invalid model response fails closed; it does not silently switch to
rules. Every interpreted edit is shown for confirmation before saving. The
OpenShift service must be deployed and reachable; adding a token does not deploy it.

Run `python app/app.py --check-services` to check the real inference path
with a synthetic natural-language reply and check the selected Yext destination.
This does not send WhatsApp or write opening hours. It reports missing setup and
returns a nonzero exit code unless AI inference and sandbox access are verified.

Source: [hours_ai.py](../app/hours_ai.py).

## ElevenLabs voice

| Mode/capability | Settings actually used |
|---|---|
| Public browser agent | An accessible `ELEVENLABS_AGENT_ID`. The browser may start by agent ID without a local API key, but this is still a live ElevenLabs session. |
| Private-agent browser session | `ELEVENLABS_API_KEY` and `ELEVENLABS_AGENT_ID` to request a WebRTC conversation token. |
| Outbound phone call | `ELEVENLABS_API_KEY`, `ELEVENLABS_AGENT_ID`, `ELEVENLABS_PHONE_NUMBER_ID`, and a destination. The sandbox demonstration uses `DEMO_OWNER_PHONE` in E.164 form. |
| Telephony provider | `ELEVENLABS_TELEPHONY` defaults to `twilio`; `sip_trunk` selects the SIP path. The phone-number ID refers to an already configured/imported provider number. |
| Agent tools | Reachable `PUBLIC_BASE_URL` and matching tool webhook URLs configured for the agent. Localhost alone is unreachable by the hosted agent. |
| Post-call webhook verification | `ELEVENLABS_WEBHOOK_SECRET`. The current handler enforces signature verification only when this secret is set. It is not required merely to open the public browser session. |
| Real Yext target | `YEXT_API_KEY`, `YEXT_BASE_URL`, `YEXT_SANDBOX_ENTITY_ID`. Synthetic dataset businesses use the voice service's separate local mock. |

Install the voice requirements and follow the
[voice startup instructions](../voice/README.md).
The supplied `start.sh` is a Bash launcher using `uv` and `ngrok`; use an appropriate
Bash/WSL environment on Windows. It starts the local mock on 8787, voice on 8000
and a public tunnel. The ngrok CLI must already be installed and able to create a
tunnel; `NGROK_DOMAIN` optionally selects its fixed domain. Browser microphone
permission is needed for speech, but keyboard interaction is also available.

The browser implementation falls back from WebRTC to WebSocket. Its WebSocket path
starts by agent ID, so private-agent access must be checked on that path separately;
the existence of a local API key does not prove every transport works. No accessible
agent means no real conversation. The demo also seeds an unanswered-text history
to exercise escalation; it does not establish that an earlier real message was sent.

Sources: [configuration](../voice/voice_service/config.py),
[API credential checks](../voice/voice_service/elevenlabs_api.py),
[session/webhook handlers](../voice/voice_service/app.py),
[browser transport](../voice/voice_service/static/console.html).

## WhatsApp transport and Yext

Actual WhatsApp sending uses `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
`TWILIO_WHATSAPP_FROM`, `TWILIO_CONTENT_SID`, `CONNECT_PUBLIC_URL` and
`CONNECT_MESSAGING_CONTACTS`. It also requires an approved sender/template and an
explicitly opted-in test recipient. Its approvals use `YEXT_API_KEY`,
`YEXT_BASE_URL` and `YEXT_SANDBOX_MIRROR` for the direct sandbox destination.
See [the complete messaging setup](../app/MESSAGING.md).

`CONNECT_PUBLIC_URL` is the WhatsApp webhook origin; `PUBLIC_BASE_URL` belongs to
the voice tools. The owner and voice apps both default to port 8000: run them
separately or configure distinct ports and matching public routes.

The direct sandbox-primary adapter requires one-to-one entity mappings. Use a
separate sandbox entity for each business, or deliberately replace the mapping
when switching between rehearsal cases.

## Configuration storage

Use the ignored root `.env` and ignored recipient/runtime files. The owner/Apply
loader supports `CONNECT_ENV_FILE`; the audited OpenShift and voice loaders read
the root `.env` directly. To use a different file consistently across all services,
provide the settings in their process environments rather than assuming that all
loaders honor `CONNECT_ENV_FILE`. Keep secrets out of source, command-line
arguments, screenshots and exported submission materials.

The submission maps only FI-D0001 to Cinnamon Swirl Café. Live event case FI-00762 requires its own sandbox entity or a deliberate remapping; the same entity cannot represent both businesses simultaneously.
