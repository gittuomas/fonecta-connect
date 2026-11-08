# Owner messaging demo

Run from the repository root with Python 3.12+:

```sh
python -m pip install -r requirements.txt
python owner-page/app.py
```

Open [Kahvila Kipinä](http://localhost:8000/w/FI-D0017/christmas) (requires the demo running locally). Approve, enter different opening hours, decline, or reply `UNDO`. The no-history customer receives suggestions without invented historical claims. The [message pack](../docs/customer-messages.md) contains the exact messages and phone screenshots.

## How it works

C13 exports supply the proposals. The app groups them by holiday and records a snapshot before an owner decides. Step 5 checks conflicts, applies the decision, reads back the result and records an audit for undo. With configured Yext credentials and `YEXT_SANDBOX_MIRROR`, mapped businesses also write to the real sandbox; otherwise the browser demo uses the supplied local simulator.

**OpenShift AI is required to evaluate conversational replies.** Copy [.env.example](../.env.example) to the repository-root `.env`, set `OPENSHIFT_AI_URL`, `OPENSHIFT_AI_MODEL` and `OPENSHIFT_AI_TOKEN`, and start the model deployment. The browser demo does not need a WhatsApp delivery key, but it does need this model connection for full natural-language interpretation.

```sh
python -c "import sys; sys.path.insert(0, 'owner-page'); import hours_ai; print(hours_ai.status())"
python owner-page/test_ai_replies.py
```

The health result should be `up`. `off` means missing URL/token; `down` means the endpoint is unavailable or credentials are invalid. Without the connection, buttons and exact edits such as `24 Dec 10-14` remain available through local rules. Conversational replies may need clarification.

## Channels and scope

`/w/<business>/<period>` shows WhatsApp, `/s/` SMS, `/e/` email, `/p/` push and `/b/` the web approval page. Browser delivery is simulated. Social-post publication and Instagram inputs are examples; they do not publish posts or fetch an owner's account. Voice runs as a [separate ElevenLabs service](../Solution/voice/README.md).

For actual WhatsApp delivery, signed inbound webhooks, durable decisions and real Yext writes, follow [MESSAGING.md](MESSAGING.md). Starting the app never sends an external message by itself. Missing transport settings leave the browser demo available.

[Live sources](../live_signals/README.md) add relevant events and ski-rental weather checks. Owner preferences and prior responses control which suggestions remain relevant. Runtime files live in ignored `.runtime` directories. Chat reset clears the conversation; use `UNDO` before resetting if you need to restore hours. Deleting local state does not undo a sandbox write.

## Verification

```sh
python -m pip install -r owner-page/requirements-messaging.txt
python -m unittest discover -s owner-page -p "test_*.py"
python -m unittest discover -s step5_apply/tests -t .
```

Offline tests cover proposals, approvals, edits, declines, undo, conflicts, preferences, live-source selection and authenticated WhatsApp transport behavior. The explicit AI-reply script above uses the configured model; actual WhatsApp delivery needs a credentialed recipient test.
