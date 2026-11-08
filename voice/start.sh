#!/usr/bin/env bash
# Start everything the voice demo needs, on this machine:
#   1. the package's Yext mock (dataset businesses)      http://127.0.0.1:8787
#   2. the Connect Voice service + demo console          http://127.0.0.1:8000
#   3. an ngrok tunnel so the ElevenLabs agent can reach the tools (PUBLIC_BASE_URL)
# Stop with Ctrl-C. Logs go to voice/data/*.log
set -euo pipefail
cd "$(dirname "$0")"
VOICE_DIR="$PWD"
REPO="$(cd .. && pwd)"
PKG="$REPO/resources"
mkdir -p data

if [ ! -d .venv ]; then
  uv venv --python 3.12 .venv >/dev/null
  uv pip install --python .venv/bin/python -r requirements.txt >/dev/null
fi

# The mock writes its state to a throw-away file so the package folder stays byte-for-byte intact
( cd "$PKG" && MOCK_STATE_FILE="$VOICE_DIR/data/mock_state.json" MOCK_QUIET=1 \
  "$VOICE_DIR/.venv/bin/python" -c "
import os, sys
sys.path.insert(0, 'mock_api')
import server
server.STATE_FILE = os.environ['MOCK_STATE_FILE']
server.STORE = server.Store()
from http.server import ThreadingHTTPServer
print('Yext mock on :8787', flush=True)
ThreadingHTTPServer(('127.0.0.1', 8787), server.Handler).serve_forever()
" ) > data/mock.log 2>&1 &
MOCK_PID=$!

# Public URL: a fixed ngrok domain if NGROK_DOMAIN is set in .env, else ngrok's default dev domain
NGROK_DOMAIN="$(grep -E '^NGROK_DOMAIN=' "$REPO/.env" 2>/dev/null | cut -d= -f2- || true)"
if [ -n "$NGROK_DOMAIN" ]; then ngrok http 8000 --url "https://$NGROK_DOMAIN" --log stdout > data/ngrok.log 2>&1 &
else ngrok http 8000 --log stdout > data/ngrok.log 2>&1 & fi
NGROK_PID=$!
sleep 3
PUBLIC_URL="$(curl -s http://127.0.0.1:4040/api/tunnels | python3 -c 'import sys,json; t=json.load(sys.stdin)["tunnels"]; print(t[0]["public_url"] if t else "")' || true)"
export PUBLIC_BASE_URL="${PUBLIC_URL:-http://localhost:8000}"
echo "Public URL for the ElevenLabs tools: $PUBLIC_BASE_URL"
echo "Demo console: http://localhost:8000"

trap 'kill $MOCK_PID $NGROK_PID 2>/dev/null' EXIT
.venv/bin/uvicorn voice_service.app:app --host 127.0.0.1 --port 8000 2>&1 | tee data/voice.log
