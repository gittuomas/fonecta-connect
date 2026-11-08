# Implementation and runtime status

Reviewed on 8 November 2026 when integrating the owner-flow fixes into main.
This project is a working challenge prototype with real service adapters, not a
fully deployed Fonecta production system. Credentials are deliberately excluded.

| Component | What actually runs | Demo data or remaining setup |
|---|---|---|
| Detect and suggest | Packaged C13 LightGBM engine; validated JSON/CSV outputs feed the owner and voice applications. | Training and business context use the supplied synthetic challenge dataset. The owner page reads frozen exports, not a fresh model prediction per chat message. Regeneration is a separate engine command. |
| Owner decision backend | Python HTTP handlers call Step 5 for approvals, confirmed edits, declines and undo. Saved-hours reads remove matching dates from new proposals; the original proposal is retained while its chat is open. | Browser conversations and draft edits are in process memory. Restart loses those chats, but not the persisted Apply audit or saved Yext hours. The local preview routes are not authenticated public customer endpoints. |
| Yext Apply | Real read/modify/write, read-back verification, audit, conflict checks and undo. FI-D0001 maps to Cinnamon Swirl Café. | `sandbox` requires the real sandbox; `auto` chooses it after a read check or uses labelled simulation; `demo` uses the challenge mock (with an optional explicit mirror). A sandbox write is not a production Google update. |
| OpenShift AI replies | Backend HTTPS inference adapter, schema validation, allowed-date checks and explicit draft confirmation. Used by both browser replies and the real messaging worker. | Live inference was **not verified** here: URL/token are absent. With neither configured, a labelled exact-format reader works. Partial configuration or service failure does not fall back to rules. Evaluators need the endpoint, inference token and matching model name; credentials alone do not deploy the model. |
| WhatsApp | Browser conversation UI plus a separate Twilio transport, authenticated webhooks, durable incoming queue, proposal references and persisted drafts. | The browser is a preview and sends nothing. Real delivery was **not verified**; it needs Twilio credentials, approved sender/template, opted-in recipient registry and public webhook routes. No automatic outreach scheduler is connected. |
| SMS, email and app push | Preview pages use the same real decision/Apply backend. Email GET links ask for confirmation before a POST mutation. | No provider transport sends SMS, email or push from these pages. “Push” here is a channel preview, separate from Git push. |
| Voice escalation | Separate FastAPI service and ElevenLabs browser/phone adapters. Its Yext writer routes the named sandbox entity to real Yext and dataset entities to a local mock. | Starts separately. Voice uses a demo clock and seeded unanswered-text history, not the browser chat's delivery ledger. Live voice and outbound calling were not verified in this integration. It requires an accessible agent; private sessions/outbound calls additionally need their credentials and reachable tools. Its audit/write implementation is separate from `step5_apply`. |
| Events and weather | Live-source adapters, freshness/provenance checks and optional generated hours-check feed. Public city feeds/Open-Meteo can run without API keys; Ticketmaster requires its key. | Ordinary challenge event context is supplied data; proactive weather uses fixtures unless live mode is enabled. Recorded replay is labelled. Live refresh is a separate command and the businesses remain synthetic. No external event automatically invents new hours. |
| Instagram hours | Caption parser and owner-approved Step 5 updates; an API fetch helper exists. | UI uses hardcoded sample captions, not a connected live Instagram account. Samples do not replace the selected sandbox holiday proposal. The fetch helper has not been live verified. |
| Social posts | Preview, text editing, approval, deletion and simulated publish log. | Google/Facebook publishing is simulated. Monthly timer advancement is a demo control; pictures are stand-in cards. No live publishing connector is wired. |
| Preferences, reminders and analytics | Preference scores, STOP/START, yearly permissions and audit state persist locally; channel and reminder decisions have implemented rules. | “No reply” and time advance controls simulate history. Browser reminder/call timelines do not send messages or launch voice. Search counts and publisher timings use challenge fixtures, not live Google analytics or publication polling. |
| Human review | FLAG and error states are surfaced for an operator. | There is no external support-ticket creation or production CRM integration. |

## Verified on the integrated code

The following verification was recorded with the [upstream integration commit](https://github.com/gittuomas/sinceAI/commit/37ad8d76e014caf12f2d4f5a4d2aac17305308cd).

- 83 owner, AI contract, messaging and live-owner-flow tests passed.
- 74 Apply/sandbox tests passed, including network failure and conflict cases.
- 32 live-source selection/preflight tests passed.
- Voice's default input now points to the checked-in C13 `voice_suggestions.json`;
  a smoke check loaded FI-D0001's four records with baseline fallback disabled.
  The legacy baseline fallback remains if a configured voice export is missing.
- A real HTTP-backend rehearsal passed all eight checks: decline, approve,
  duplicate approval, undo approval, draft edit, cancel, confirm edit and undo edit.
  Full Yext hours were independently read back and the original object restored.
- Desktop and phone browser checks covered a typed draft, explicit save and undo.
- The read-only service check reported `apply_target=sandbox` and
  `sandbox_read_verified=true`, with AI and real messaging unconfigured. Its
  nonzero exit accurately indicates incomplete live-service readiness.

The final submission package reran these same 189 automated tests successfully after integration. Its mobile preview was checked for proposal, approval, edit draft, confirmation, decline and undo; the message screenshots were refreshed. The real sandbox checks above are the upstream verification record, not a new credentialed run during packaging.

Unit tests use doubles where appropriate; they do not establish live OpenShift,
Twilio or ElevenLabs availability. The eight sandbox checks used real Yext.

## Evaluator entry points

1. Copy `.env.example` to ignored `.env`; select `CONNECT_APPLY_MODE=sandbox` for
   the real FI-D0001 rehearsal, or `demo` for the full synthetic roster.
2. Supply OpenShift URL/token/model for natural-language replies and Yext
   credentials plus the unique FI-D0001 mapping for real writes.
3. Run `python app/app.py --check-services`, then `python app/app.py`.
4. Open `/w/FI-D0001/christmas` in sandbox mode. If the proposed hours are already
   saved, “No update needed” is intentional. Reset never erases those hours.
5. To repeat the reversible live test, run
   `python app/rehearse_sandbox.py --run`. This temporarily writes to Yext,
   verifies each step and restores the original hours. Avoid concurrent edits to
   that sandbox entity during the rehearsal.

FI-00762 is a separate live-event example. It needs another sandbox entity or a
deliberate replacement of the FI-D0001 mapping; mapping both businesses to one
entity is rejected. See [service configuration](configuration.md),
[real WhatsApp setup](../app/MESSAGING.md),
[voice setup](../voice/README.md), and
[live-source setup](../live_signals/README.md).
