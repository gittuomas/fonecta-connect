"""Settings for the voice escalation service.

Everything comes from environment variables. The repo-root .env (gitignored) is loaded
automatically, so the Yext and ElevenLabs keys never have to appear in code or in a shell
history. See .env.example for the variable names.
"""
from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent            # voice/voice_service
VOICE_DIR = HERE.parent                           # voice
REPO_ROOT = VOICE_DIR.parent               # repo root


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


_load_dotenv(REPO_ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


PKG_DIR = Path(env("PKG_DIR", str(REPO_ROOT / "resources")))
DATA_DIR = Path(env("VOICE_DATA_DIR", str(VOICE_DIR / "data")))

# Public URL of this service (the ngrok domain). The ElevenLabs tools call PUBLIC_BASE_URL + /voice/tools/...
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

# ElevenLabs
ELEVENLABS_API_KEY = env("ELEVENLABS_API_KEY")          # only needed for phone calls, batch calls, signed URLs
ELEVENLABS_AGENT_ID = env("ELEVENLABS_AGENT_ID")
ELEVENLABS_PHONE_NUMBER_ID = env("ELEVENLABS_PHONE_NUMBER_ID")   # set after a Twilio number is imported
ELEVENLABS_TELEPHONY = env("ELEVENLABS_TELEPHONY", "twilio")     # twilio | sip_trunk
ELEVENLABS_WEBHOOK_SECRET = env("ELEVENLABS_WEBHOOK_SECRET")     # HMAC secret of the post-call webhook
ELEVENLABS_API_BASE = env("ELEVENLABS_API_BASE", "https://api.elevenlabs.io").rstrip("/")

# Yext: the team's sandbox business, plus the local mock for the dataset businesses
YEXT_SANDBOX_BASE = env("YEXT_BASE_URL", "https://sbx-api.yextapis.com").rstrip("/")
YEXT_SANDBOX_KEY = env("YEXT_API_KEY")
YEXT_SANDBOX_ENTITY_ID = env("YEXT_SANDBOX_ENTITY_ID", "Cinnamon Swirl Café")
YEXT_MOCK_BASE = env("YEXT_MOCK_BASE", "http://127.0.0.1:8787").rstrip("/")
YEXT_API_VERSION = env("YEXT_API_VERSION", "20261106")

# The sandbox business has no history in the dataset, so for the demo it borrows the
# suggestion history of one dataset business (Fonecta's own Kahvila Aurora example).
SANDBOX_STANDIN_FOR = env("SANDBOX_STANDIN_FOR", "FI-D0001")
SANDBOX_CITY = env("SANDBOX_CITY", "Helsinki")
SANDBOX_COUNTRY = env("SANDBOX_COUNTRY", "FI")
DEMO_OWNER_NAME = env("DEMO_OWNER_NAME", "Max")
DEMO_OWNER_LANGUAGE = env("DEMO_OWNER_LANGUAGE", "en")
DEMO_OWNER_PHONE = env("DEMO_OWNER_PHONE")          # E.164, only used in phone mode
# Fonecta asked for the demo in English. Set to "" to call every owner in their own language (fi/sv/nb/en).
DEMO_FORCE_LANGUAGE = env("DEMO_FORCE_LANGUAGE", "en")

# Escalation policy knobs (see policy.py)
VOICE_WAIT_HOURS = float(env("VOICE_WAIT_HOURS", "24"))          # wait after an unanswered text before calling
VOICE_WAIT_HOURS_URGENT = float(env("VOICE_WAIT_HOURS_URGENT", "4"))
VOICE_URGENT_DAYS = int(env("VOICE_URGENT_DAYS", "10"))          # first date this close = urgent
VOICE_MIN_CONFIDENCE = float(env("VOICE_MIN_CONFIDENCE", "0.6"))
VOICE_MAX_ATTEMPTS = int(env("VOICE_MAX_ATTEMPTS", "2"))
VOICE_MIN_GAP_HOURS = float(env("VOICE_MIN_GAP_HOURS", "4"))
VOICE_MAX_ITEMS = int(env("VOICE_MAX_ITEMS", "4"))
VOICE_HORIZON_DAYS = int(env("VOICE_HORIZON_DAYS", "21"))       # dates further out wait for the next round

# Demo clock: the dataset's "today" is 6 Nov 2026, but voice escalation is about the last
# weeks before a holiday, so the demo runs as if it were this moment (local Finnish time).
DEMO_NOW = env("DEMO_NOW", "2026-12-15T12:30:00")

# Where engine suggestion objects land (handoff section 8 schema). Falls back to the baseline.
SUGGESTIONS_PATH = Path(env("SUGGESTIONS_PATH", str(REPO_ROOT / "engine" / "output" / "voice_suggestions.json")))
