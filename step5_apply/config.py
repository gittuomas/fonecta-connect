"""Paths and settings. Secrets come from the environment or a gitignored `.env`, never from code."""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MATERIALS = REPO_ROOT / "MATERIALS FROM SINCE AI (MAIN CHALLENGE)"
DATA = MATERIALS / "data"
PACKAGE_DIR = Path(__file__).resolve().parent
RUNTIME = PACKAGE_DIR / ".runtime"        # audit log, standing permissions, mock state (gitignored)

# Yext: keep the version date FIXED (docs: "use a fixed date, never a dynamic one").
YEXT_V = "20261106"
SANDBOX_URL = "https://sbx-api.yextapis.com"
MOCK_URL = "http://127.0.0.1:8787"


def load_dotenv(path: Path | None = None) -> None:
    """Read KEY=VALUE lines from `.env` (repo root, or $CONNECT_ENV_FILE). Never overrides the real environment."""
    path = Path(os.environ.get("CONNECT_ENV_FILE", path or REPO_ROOT / ".env"))
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def sandbox_key() -> str | None:
    return os.environ.get("YEXT_API_KEY") or None


def sandbox_url() -> str:
    return os.environ.get("YEXT_BASE_URL", SANDBOX_URL)


def sandbox_mirror() -> dict[str, str]:
    """YEXT_SANDBOX_MIRROR="FI-D0001:Cinnamon Swirl Café" -> {"FI-D0001": "Cinnamon Swirl Café"}.

    Writes approved for the mock business on the left are also written to the sandbox business on the right,
    so the demo shows a real Yext write while detection runs on the mock's history.
    """
    out = {}
    for pair in os.environ.get("YEXT_SANDBOX_MIRROR", "").split(","):
        if ":" in pair:
            a, b = pair.split(":", 1)
            out[a.strip()] = b.strip()
    return out
