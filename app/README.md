# Owner messaging demo

Run from the repository root with Python 3.12+:

```sh
python -m pip install -r requirements.txt
python app/app.py
```

Open [Kahvila Kipinä](http://localhost:8000/w/FI-D0017/christmas) (requires the demo running locally). Approve, enter different opening hours, decline, or reply `UNDO`. The no-history customer receives suggestions without invented historical claims. The [message pack](../docs/customer-messages.md) contains the exact messages and phone screenshots.

## How it works

C13 exports supply the proposals. The app groups them by holiday and records a snapshot before an owner decides. Step 5 checks conflicts, applies the decision, reads back the result and records an audit for undo. `CONNECT_APPLY_MODE=auto` prefers a read-verified sandbox and otherwise selects labelled local simulation. Use `sandbox` to require real writes for the uniquely mapped FI-D0001 business, or `demo` for the full synthetic roster. Matching saved dates are omitted from new proposals. Typed edits produce a draft; **Save these hours / CONFIRM** writes it, and **Cancel** discards it.

**OpenShift AI is required to evaluate conversational replies.** Copy [.env.example](../.env.example) to the repository-root `.env`, set `OPENSHIFT_AI_URL`, `OPENSHIFT_AI_MODEL` and `OPENSHIFT_AI_TOKEN`, and start the model deployment. The browser demo does not need a WhatsApp delivery key, but it does need this model connection for full natural-language interpretation.

```sh
python app/app.py --check-services
```

The check makes a synthetic inference request and reads Yext without changing hours or sending messages. A nonzero exit means one or both live services remain unverified. Leave both AI URL and token empty for labelled exact-format parsing. Partial configuration or service failure saves nothing and asks for clarification. Both browser and real WhatsApp use this parser.

## Channels and scope

`/w/<business>/<period>` shows WhatsApp, `/s/` SMS, `/e/` email, `/p/` push and `/b/` the web approval page. Browser delivery is simulated. Social-post publication and Instagram inputs are examples; they do not publish posts or fetch an owner's account. Voice runs as a [separate ElevenLabs service](../voice/README.md).

For actual WhatsApp delivery, signed inbound webhooks, durable decisions and real Yext writes, follow [MESSAGING.md](MESSAGING.md). Starting the app never sends an external message by itself. Missing transport settings leave the browser demo available.

[Live sources](../live_signals/README.md) add relevant events and ski-rental weather checks. Owner preferences and prior responses control which suggestions remain relevant. Runtime files live in ignored `.runtime` directories. Chat reset clears the conversation; use `UNDO` before resetting if you need to restore hours. Deleting local state does not undo a sandbox write.

## Verification

```sh
python -m pip install -r app/requirements-messaging.txt
python -m unittest discover -s app -p "test_*.py"
python -m unittest discover -s step5_apply/tests -t .
```

Offline tests cover proposals, approvals, edits, declines, undo, conflicts, preferences, live-source selection and authenticated WhatsApp transport behavior. The explicit AI-reply script above uses the configured model; actual WhatsApp delivery needs a credentialed recipient test.

See the [runtime overview](../docs/runtime-status.md), [service configuration](../docs/configuration.md) and [recorded sandbox verification](../docs/owner-flow-verification.md).
