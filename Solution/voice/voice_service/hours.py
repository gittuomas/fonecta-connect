"""Opening-hours strings: parse what the voice agent sends, validate, convert for Yext.

Canonical form (same as the challenge package):
    "closed" | "09:00-17:00" | "10:00-14:00,17:00-22:00" | "18:00-02:00" | "00:00-23:59"

The agent is told to send canonical strings, but speech-to-text and LLMs drift, so the
parser also accepts "10-15", "10.00–15.00", "10:00 to 15:00", "kiinni", "stängt", "24h".
Anything it can't read comes back as a ValueError with a sentence the agent can act on.
"""
from __future__ import annotations

import re

CLOSED_WORDS = {"closed", "kiinni", "suljettu", "stängt", "stengt", "stängd", "stengt hele dagen", "closed all day"}
H24_WORDS = {"24h", "24 h", "24/7", "open 24 hours", "00:00-24:00", "0-24", "00-24", "ympäri vuorokauden"}

_TIME = r"(\d{1,2})(?:[:.](\d{2}))?"
_RANGE = re.compile(rf"^\s*{_TIME}\s*(?:-|–|—|to|until|till|tills|til|asti|–)\s*{_TIME}\s*$", re.I)


def _t(h: str, m: str | None) -> str:
    hh, mm = int(h), int(m or 0)
    if hh == 24 and mm == 0:
        return "23:59"
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"{h}:{m or '00'} is not a valid time of day")
    return f"{hh:02d}:{mm:02d}"


def normalize(raw: str) -> str:
    """Return the canonical hours string or raise ValueError."""
    if raw is None:
        raise ValueError("No hours were given. Ask the owner for the opening and closing time, or whether they are closed.")
    s = str(raw).strip().lower().replace("klo", "").replace("kl.", "").replace("kl", "").strip()
    s = s.replace("–", "-").replace("—", "-")
    if s in CLOSED_WORDS:
        return "closed"
    if s in H24_WORDS:
        return "00:00-23:59"
    parts = [p.strip() for p in re.split(r"[,;]| and | ja | och | og ", s) if p.strip()]
    if not parts:
        raise ValueError("The hours were empty.")
    out = []
    for p in parts:
        m = _RANGE.match(p)
        if not m:
            raise ValueError(f"Could not read '{raw}'. Send the hours as HH:MM-HH:MM in 24-hour time, or 'closed'.")
        a, b = _t(m.group(1), m.group(2)), _t(m.group(3), m.group(4))
        if a == b:
            raise ValueError("Opening and closing time are the same. Ask the owner again.")
        out.append((a, b))
    out.sort()
    for (a1, b1), (a2, _) in zip(out, out[1:]):
        if b1 > a1 and a2 < b1:
            raise ValueError("The time ranges overlap. Ask the owner to repeat the hours.")
    return ",".join(f"{a}-{b}" for a, b in out)


def to_yext(h: str) -> dict:
    """Canonical string -> Yext holidayHours fields (without the date)."""
    if h == "closed":
        return {"isClosed": True}
    return {"openIntervals": [{"start": a, "end": b} for a, b in (iv.split("-") for iv in h.split(","))]}


def from_yext(obj: dict | None) -> str:
    if not obj or obj.get("isClosed"):
        return "closed"
    ivs = obj.get("openIntervals") or []
    return ",".join(f"{i['start']}-{i['end']}" for i in ivs) or "closed"


def pretty(h: str) -> str:
    """'07:30-13:00' -> '07:30–13:00' (en dash, easier for TTS and humans)."""
    if h in ("closed", "", None):
        return "closed"
    if h == "REGULAR":
        return "regular hours"
    return ", ".join(iv.replace("-", "–") for iv in h.split(","))


def same_state(a: str, b: str) -> bool:
    return (a == "closed") == (b == "closed")
