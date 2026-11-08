# Fonecta Connect Voice

When an important opening-hours request goes unanswered, the ElevenLabs voice agent follows up, confirms each date and saves the owner's approved hours. The demo starts from a seeded unanswered-message scenario.

## Run

Configure the repository-root `.env` from [.env.example](../../.env.example). An accessible `ELEVENLABS_AGENT_ID` is required for the browser conversation. Private agents also need `ELEVENLABS_API_KEY`. Actual outbound calls need that key, `ELEVENLABS_PHONE_NUMBER_ID` and `DEMO_OWNER_PHONE`. Yext sandbox writes require `YEXT_API_KEY`, `YEXT_BASE_URL` and `YEXT_SANDBOX_ENTITY_ID`.

On macOS/Linux with `uv` and an authenticated `ngrok` installed:

```sh
cd Solution/voice
./start.sh
```

This starts the local Yext simulator on 8787, voice service on 8000 and an ngrok tunnel. The owner app also defaults to 8000, so stop it before starting this launcher.

For a manual launch on any platform, first install dependencies and start the simulator from the repository root in one terminal:

```sh
python -m pip install -r Solution/voice/requirements.txt
python -c "import os,sys; from pathlib import Path; from http.server import ThreadingHTTPServer; Path('Solution/voice/data').mkdir(exist_ok=True); os.environ['MOCK_STATE_FILE']='Solution/voice/data/mock_state.json'; sys.path.insert(0,'MATERIALS FROM SINCE AI (MAIN CHALLENGE)/mock_api'); import server; server.STATE_FILE=os.environ['MOCK_STATE_FILE']; server.STORE=server.Store(); ThreadingHTTPServer(('127.0.0.1',8787),server.Handler).serve_forever()"
```

In another terminal, from `Solution/voice`, run:

```sh
python -m uvicorn voice_service.app:app --host 127.0.0.1 --port 8000
```

Expose the voice service through your configured tunnel and set `PUBLIC_BASE_URL` to its public HTTPS URL before launching. Set up the agent with [system_prompt.md](agent/system_prompt.md) and the [tool definitions](agent/tools.json), pointing webhook URLs at that public service. `ELEVENLABS_WEBHOOK_SECRET` enables verification of post-call callbacks.

Open the [voice console](http://localhost:8000/) (requires the voice service running locally), choose a business, select **Call now**, then **Answer** and allow the microphone. The type-instead-of-speaking option uses the same agent. The default suggestion input is `Solution/engine/output/voice_suggestions.json`.

The console's **Reset demo** also removes hours this demo wrote to the configured sandbox; use it deliberately. Publisher names and estimated propagation times come from the challenge data, not independent checks of Google publication.

## Tests

From `Solution/voice`:

```sh
python tests/test_flow.py
```

This verifies approval, edit and decline against a local Yext simulator without making an ElevenLabs call. The [conversation evaluator](http://localhost:8000/eval) (requires the voice service and a configured agent) exercises the real agent and uses ElevenLabs credits. Runtime state and logs stay in the ignored `data/` directory.
