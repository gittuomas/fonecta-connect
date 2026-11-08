# Real WhatsApp messaging

The project can send actual WhatsApp proposals through Twilio, receive authenticated owner replies, apply approved hours directly to the real Yext sandbox, and send actual confirmations. There is no mock transport or fallback sender. Without configuration, the app says messaging is unconfigured and sends nothing. The `/w/...` browser screen is explicitly a local preview.

The browser demonstration remains available without any credentials or Twilio
installation. Missing or invalid messaging setup automatically keeps the local
demo running instead of stopping startup. Its normal success/undo copy is preserved;
mode information lives in the operator panel and a small preview footer. With no
working transport setup, `--send-whatsapp` returns `preview_ready`, `sent: false`
and the local preview URL. It does not pretend that a provider received a message.
If an actual provider request fails or times out, its result still needs review;
no fake delivery or sandbox write is substituted afterwards.

This implementation has offline tests. **Live delivery, template approval and real-device replies have not been verified with project credentials.** Setting credentials alone is not sufficient: a WhatsApp sender, an approved content template, an opted-in recipient and reachable HTTPS webhooks are also required.

## Setup

1. Use Python 3.12+ and install `python -m pip install -r app/requirements-messaging.txt` (tested with Twilio SDK 9.13.0).
2. Register a WhatsApp sender in Twilio. Twilio's WhatsApp sandbox also delivers actual messages to joined test recipients, but its preapproved templates do not necessarily match this application's hours template; a registered sender plus approved template is the straightforward full-flow setup.
3. Create the English `twilio/quick-reply` content template specified in [whatsapp-template.json](whatsapp-template.json), using Twilio's Content Template Builder or Content API. Submit it for WhatsApp approval, then put its `HX...` content SID in `.env`. Keep the four variable positions and button payloads exact. Template approval is a provider decision, not a claim made by this repository. The sender identity must be authorized by the team/Fonecta.
4. Configure the variables below in the ignored repository `.env`. Use the real primary Twilio Auth Token for webhook validation. Never put secrets into URLs or tracked files.

```dotenv
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_WHATSAPP_FROM=whatsapp:+...
TWILIO_CONTENT_SID=HX...
CONNECT_PUBLIC_URL=https://your-public-host.example
CONNECT_MESSAGING_CONTACTS=app/.runtime/messaging-contacts.json
YEXT_BASE_URL=https://sbx-api.yextapis.com
YEXT_API_KEY=...
YEXT_SANDBOX_MIRROR=FI-D0001:Cinnamon Swirl Café
```

5. Create the ignored contacts file. This is an explicit real-recipient registry, separate from synthetic challenge contacts. Replace the placeholder with a recipient who has opted in to this demonstration. Each phone number must belong to exactly one business, and each business must map to a distinct sandbox entity.

```json
{
  "FI-D0001": {"address": "whatsapp:+<real E.164 number>", "opted_in": true}
}
```

6. Run `python app/app.py`. Use a TLS reverse proxy/tunnel that exposes **only** `POST /messaging/inbound` and `POST /messaging/status` (including the status query string) to the app's localhost port 8000. The old owner/demo routes are operator previews, not authenticated public endpoints. Do not expose the whole server. Configure the WhatsApp sender's inbound webhook as `https://your-public-host.example/messaging/inbound`, method POST. The app supplies the delivery-status callback on every send automatically. Preserve form bodies and URLs exactly; the SDK validates signatures against `CONNECT_PUBLIC_URL`, not untrusted forwarding headers.
7. Keep this app process running as the single messaging worker. In another shell, send a chosen eligible proposal:

```sh
python app/app.py --send-whatsapp FI-D0001 christmas
python app/app.py --messaging-status
```

Sending is an explicit operator action. Starting the app does not send proposals. Current implementation does not automatically schedule preferred contact windows, send reminders or escalate to voice; operators must choose an appropriate contact time. Explicit WhatsApp opt-in chooses this channel; it is not an implementation of the contact plan's learned channel selection.

## Owner interaction and real Apply

Template variables carry the selected prediction, the owner's matching historical hours when available, and a unique proposal reference. Buttons return `YES <reference>`, `NO <reference>` and `OTHER <reference>`. Typed replies support those commands, `EDIT <reference> 24 Dec 10-14` (one day per line), and `UNDO <reference>`. The reference prevents a delayed reply from approving a different proposal. Unknown numbers cannot access a proposal.

After **Other hours**, plain owner text is accepted when exactly one authenticated
proposal is awaiting an edit. It uses the same OpenShift backend parser as the
browser preview. The worker replies with a draft and asks for `CONFIRM <reference>`
or `CANCEL <reference>`; plain confirmation is also accepted for one waiting draft.
Nothing is written at the parsing step. Drafts survive worker restarts. If multiple
proposals are waiting, the reference is required. Missing AI credentials permit
only the documented exact date/time format; configured model failures save nothing.

New outreach checks current sandbox exceptions, omits matching days, and checks
again before sending. A send-time race stops the send instead of claiming that
already-saved hours are missing. Existing sent proposals keep their original
snapshot for conflict detection.

The hours, real Yext snapshot and recipient are frozen at send time. Approval/edit/decline reuse Step 5 with **sandbox as the primary destination**, not the local mock or optional mirror. Real messages use their own audit and permissions under `app/.runtime/messaging/`. Web preview state is separate. Snapshot conflict checks preserve intervening owner changes. Undo is scoped to the proposal's audit record. Partial/unreadable hours are rejected without applying the readable subset. Proposals expire after seven days and cannot apply past dates.

The actual outbound confirmation lists verified sandbox dates and failures. It never uses the fixture-based publisher list to claim that Google or other live publishers changed. Production publisher propagation is still unverified.

`STOP` immediately persists a messaging opt-out, including before an already queued approval is processed. `START` clears that messaging opt-out; it does not override the challenge/business contact policy's opt-out. There is no implicit standing permission from a WhatsApp reply.

## Delivery, restarts and recovery

Signed inbound webhooks are persisted before acknowledgement and processed by the worker, so slow Yext calls do not block webhook acknowledgement. Incoming MessageSids are deduplicated; repeat approval commands cannot apply a completed proposal again. SQLite also keeps outgoing operation IDs, provider SIDs and status callbacks. `queued`/`sent` mean provider states, not delivery; `delivered`/`read` come only from authenticated callbacks.

REST send timeouts are ambiguous: Twilio might already have accepted the message. They become `needs_review`, with **no automatic resend**. A duplicate send command for a business/period is blocked. Inbound failures or a crash during processing also require review rather than an automatic replay of external effects. If a decision succeeded but its reply failed, the audit remains intact. The status command identifies affected operations without printing recipient numbers, message bodies or credentials.

Run one worker for each runtime directory. Do not share the SQLite runtime among independently deployed worker processes. Preserve the directory across normal restarts. Restrict filesystem access: it contains personal data, proposal snapshots and audit records. Follow-up replies use the actual inbound conversation window; work queued for 23 hours is held for review instead of sending an out-of-window freeform message.

For `needs_review` or a stranded `processing`/`submitting` operation, compare the stored SID/operation with Twilio's message logs and the messaging `audit.jsonl`, then inspect the real sandbox entity before deciding whether anything needs retrying. There is intentionally no blind retry/reset command. For a fresh controlled demonstration, stop the worker, verify/undo real sandbox changes, archive the runtime securely, and use a new `CONNECT_MESSAGING_RUNTIME` path. Deleting runtime state does not undo a real Yext change or unsend a message.

## Verification

```sh
python -m unittest discover -s app -p test_messaging.py -v
```

These tests use explicit isolated test doubles. They cover SDK signature verification, unknown senders, frozen snapshots, duplicate/restarted delivery, exact edits, decline, scoped undo, STOP/START, eligibility, expiry, ambiguous failures and callback ordering. No real message is sent by the test suite.

The remaining credentialed acceptance check is: send to an opted-in test phone; inspect real WhatsApp buttons; approve/edit/decline; verify actual Yext read-back and undo; observe delivery callbacks; test STOP and invalid webhook signatures through the configured proxy. Do this before describing the feature as live-tested.

Provider references: [WhatsApp send/receive](https://www.twilio.com/docs/whatsapp/quickstart), [quick-reply template contract](https://www.twilio.com/docs/content/twilio-quick-reply), [webhook security](https://www.twilio.com/docs/usage/security), [incoming fields](https://www.twilio.com/docs/messaging/guides/webhook-request). The Twilio Python SDK is MIT licensed; its dependency licences must be included in the submission dependency audit.
