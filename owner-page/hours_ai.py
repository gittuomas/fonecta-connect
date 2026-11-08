"""Read an owner's free-text reply with the team's AI model on Red Hat OpenShift AI.

The model (Llama 3.2 1B Instruct from Red Hat's modelcar catalog, served by vLLM on CPU in
OpenShift AI; deployment name "granite-hours") turns a reply such as
"closed on Christmas Eve, but open till 8 on the 23rd" into structured hours per date.
Its answer is then checked by plain code before anything reaches step 5 (Apply):
only dates in this message, only valid times, nothing else.

Settings (in the repository's .env file, never committed):
    OPENSHIFT_AI_URL    the model's external inference endpoint, e.g. https://granite-hours-....openshiftapps.com
    OPENSHIFT_AI_TOKEN  the deployment's access token
    OPENSHIFT_AI_MODEL  served model name (default: granite-hours)

If the model isn't configured or doesn't answer, `read_reply` returns None and the
owner page falls back to its simple pattern reader. Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request

LAST_AI_PARTS = 0   # how many parts of the last reply the AI model read (shown on the demo panel)
TIME = re.compile(r"^([01]?\d|2[0-4]):([0-5]\d)$")


def load_env():
    """Read KEY=value lines from the repository's .env (only keys not already set)."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def configured() -> bool:
    load_env()
    return bool(os.environ.get("OPENSHIFT_AI_URL") and os.environ.get("OPENSHIFT_AI_TOKEN"))


def status() -> str:
    """'off' (no token in .env), 'up' (the model answers) or 'down' (configured but not answering)."""
    if not configured():
        return "off"
    req = urllib.request.Request(os.environ["OPENSHIFT_AI_URL"].rstrip("/") + "/v1/models",
                                 headers={"Authorization": "Bearer " + os.environ["OPENSHIFT_AI_TOKEN"]})
    try:
        with urllib.request.urlopen(req, timeout=15, context=_ssl_context()) as r:
            return "up" if r.status == 200 else "down"
    except Exception:  # noqa: BLE001  (any failure means it isn't answering)
        return "down"


SYSTEM = (
    "You read one short message from a shop owner about their opening hours on one day, and fill in a form. "
    "Use the 24-hour clock. Shops open in the morning or at midday and close in the afternoon or evening, "
    "so a closing time like '4', '6' or '8' means 16:00, 18:00 or 20:00, and 'noon' is 12:00. "
    "status: 'closed' if they are closed, 'regular' if they say normal, usual or regular hours, 'open' otherwise. "
    "Leave open or close empty when the message doesn't say it."
)
EXAMPLES = [   # (owner's text, answer): small models learn the format best from examples
    ("closed that day", {"status": "closed", "open": "", "close": ""}),
    ("12 to 4 please", {"status": "open", "open": "12:00", "close": "16:00"}),
    ("we're open until 8", {"status": "open", "open": "", "close": "20:00"}),
    ("9-12", {"status": "open", "open": "09:00", "close": "12:00"}),
    ("we open at noon", {"status": "open", "open": "12:00", "close": ""}),
    ("close at 6pm", {"status": "open", "open": "", "close": "18:00"}),
    ("normal hours", {"status": "regular", "open": "", "close": ""}),
    ("10:30-15", {"status": "open", "open": "10:30", "close": "15:00"}),
]
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["status", "open", "close"], "properties": {
    "status": {"type": "string", "enum": ["open", "closed", "regular"]},
    "open": {"type": "string", "pattern": "^(([01][0-9]|2[0-3]):[0-5][0-9])?$"},
    "close": {"type": "string", "pattern": "^(([01][0-9]|2[0-3]):[0-5][0-9])?$"}}}


ALIASES = {   # extra words owners use for a day, by the challenge's day_key
    "XMAS_EVE": ["christmas eve", "xmas eve"], "XMAS_DAY": ["christmas day", "xmas day", "christmas"],
    "BOXING_DAY": ["boxing day", "st stephen"], "DEC23": ["day before christmas eve"],
    "NYE": ["new year's eve", "new years eve", "nye"], "NEW_YEARS_DAY": ["new year's day", "new years day"],
    "EPIPHANY_EVE": ["epiphany eve"], "EPIPHANY": ["epiphany"], "INDEPENDENCE_DAY": ["independence day"],
    "FATHERS_DAY": ["father's day", "fathers day"], "LUCIA": ["lucia", "lucy"], "BLACK_FRIDAY": ["black friday"],
}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def label_parts(text, items):
    """Split the reply into parts and find which day each part is about, with plain rules.
    Returns [(date or None, part)]. The AI model then reads the hours from each part."""
    parts = [p.strip() for p in re.split(r"[,;.\n]+|\bbut\b|\band\b|\balso\b", text, flags=re.I) if p.strip()]
    out = []
    for part in parts:
        low = part.lower()
        hit = None
        for s in items:     # holiday names first, longest phrases first
            for alias in sorted(ALIASES.get(s["key"], []) + [s["day_name"].lower()], key=len, reverse=True):
                if alias and alias in low:
                    hit = hit or s
        if hit is None:
            for s in items:
                d = s["day"].day
                if re.search(rf"\b{d}(st|nd|rd|th)?\b", low) or re.search(rf"\b{d}\.?\s*{s['day']:%b}".lower(), low) \
                        or WEEKDAYS[s["day"].weekday()] in low:
                    hit = s
                    break
        if hit is None and len(items) == 1:
            hit = items[0]
        out.append((hit["day"].isoformat() if hit else None, part))
    # a part with no day of its own ("open until 8") belongs to the day named just before it
    for i in range(1, len(out)):
        if out[i][0] is None and out[i - 1][0]:
            out[i] = (out[i - 1][0], out[i][1])
    return out


def _messages(part):
    msgs = [{"role": "system", "content": SYSTEM}]
    for text, answer in EXAMPLES:
        msgs += [{"role": "user", "content": text}, {"role": "assistant", "content": json.dumps(answer)}]
    return msgs + [{"role": "user", "content": part}]


def _ssl_context():
    # python.org builds of Python on macOS ship without certificates; fall back to the system's list.
    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs() and os.path.exists("/etc/ssl/cert.pem"):
        ctx = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return ctx


def _call(part, timeout=120):
    """Ask the model about one part of the reply. Returns {"status", "open", "close"}."""
    url = os.environ["OPENSHIFT_AI_URL"].rstrip("/") + "/v1/chat/completions"
    body = {
        "model": os.environ.get("OPENSHIFT_AI_MODEL", "granite-hours"),
        "messages": _messages(part),
        "temperature": 0,
        "max_tokens": 60,
        "response_format": {"type": "json_schema", "json_schema": {"name": "hours", "schema": SCHEMA, "strict": True}},
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + os.environ["OPENSHIFT_AI_TOKEN"],
    })
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
        out = json.loads(r.read().decode("utf-8"))
    raw = out["choices"][0]["message"]["content"]
    return json.loads(raw[raw.find("{"): raw.rfind("}") + 1])


def _valid_hours(h):
    if h in ("closed", "regular"):
        return True
    parts = h.split("-")
    if len(parts) != 2 or not all(TIME.match(p) for p in parts):
        return False
    return parts[0] != parts[1]


def _first_interval(hours):
    if hours in ("", "closed", "regular", None):
        return None, None
    a, b = hours.split(",")[0].split("-")
    return a, b


def _hh(h, m, evening=False):
    h = int(h)
    if evening and h < 12:
        h += 12
    return f"{h:02d}:{m or '00'}"


def rule_definite(part):
    """The plain-rule answer for a part when the owner's words leave no doubt, else None."""
    r = rule_check(part, {"status": "open", "open": "", "close": ""})
    return r if r["status"] != "open" or (r["open"] and r["close"]) else None


def rule_check(part, ai):
    """Safety check on the AI's answer for one part of the reply. Clear facts in the owner's own words win:
    'closed', 'normal hours', and written-out ranges like '10-14'. Any time the AI gives must match a number
    the owner actually wrote (or 'noon'/'midnight'); otherwise it's dropped and the owner gets asked."""
    low = part.lower()
    if re.search(r"\bclosed\b|\bshut\b|\bnot open\b", low) and not re.search(r"\d", re.sub(r"\b\d{1,2}(st|nd|rd|th)\b|\b\d{1,2}\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*", "", low)):
        return {"status": "closed", "open": "", "close": ""}
    if re.search(r"\b(normal|usual|regular)\b", low):
        return {"status": "regular", "open": "", "close": ""}
    clean = re.sub(r"\b\d{1,2}(st|nd|rd|th)\b|\b\d{1,2}\.?\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*", " ", low)
    m = re.search(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(?:am|pm)?\s*(?:-|–|to|till|until)\s*(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?", clean)
    if m:
        o = _hh(m.group(1), m.group(2))
        c = _hh(m.group(3), m.group(4), evening=int(m.group(3)) <= int(m.group(1)) or m.group(5) == "pm")
        return {"status": "open", "open": o, "close": c}
    said = {int(x) for x in re.findall(r"(?<!\d)(\d{1,2})(?:[:.]\d{2})?(?!\d)", clean)}
    if "noon" in low or "midday" in low:
        said.add(12)
    if "midnight" in low:
        said.add(0)
    ok = lambda t: (not t) or int(t[:2]) in said or int(t[:2]) - 12 in said   # noqa: E731
    out = {"status": ai.get("status", "open"), "open": ai.get("open", "") if ok(ai.get("open", "")) else "",
           "close": ai.get("close", "") if ok(ai.get("close", "")) else ""}
    if ("noon" in low or "midday" in low) and not out["open"] and not out["close"]:   # "open at noon"
        out["open" if "open" in low else "close"] = "12:00"
    return out


def read_reply(text, items):
    """Return (found, unclear) with found = {date: hours}, or None if the model isn't available.
    Plain rules find which day each part of the reply is about; the AI model reads the hours in each part
    (parts are sent in parallel). Results are checked; a missing opening or closing time is filled from the
    suggestion (or the regular hours) for that day."""
    from concurrent.futures import ThreadPoolExecutor
    if not configured():
        return None
    parts = [(d, p) for d, p in label_parts(text, items) if d]
    if not parts:
        return {}, "Which day do you mean? For example \"24 Dec closed\"."
    # Only parts the plain rules can't read for sure go to the AI model ("open until 8", "from noon");
    # clear ones ("closed", "normal hours", "12 to 4") are answered at once. Saves 7-8 s per part on CPU.
    sure = [rule_definite(p) for _, p in parts]
    talks_hours = lambda p: re.search(r"\d|noon|midday|midnight|open|clos|shut", p.lower())   # noqa: E731
    ask = [i for i, r in enumerate(sure) if r is None and talks_hours(parts[i][1])]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:   # one at a time: faster on the sandbox's 2 CPUs
            got = dict(zip(ask, pool.map(lambda i: _call(parts[i][1]), ask)))
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, OSError):
        return None
    answers = [sure[i] if sure[i] is not None else got.get(i) for i in range(len(parts))]
    global LAST_AI_PARTS
    LAST_AI_PARTS = len(ask)
    by_date = {s["day"].isoformat(): s for s in items}
    merged = {}   # date -> {"status", "open", "close"}, combining parts about the same day
    for (d, part), a in zip(parts, answers):
        if a is None:          # a part without any hours in it ("rest is fine")
            continue
        a = rule_check(part, a)
        m = merged.setdefault(d, {"status": "open", "open": "", "close": ""})
        if a.get("status") in ("closed", "regular"):
            m["status"] = a["status"]
        m["open"] = m["open"] or a.get("open", "")
        m["close"] = m["close"] or a.get("close", "")
    found, unclear = {}, ""
    for d, m in merged.items():
        s = by_date[d]
        if m["status"] != "open" and not (m["open"] or m["close"]):
            found[s["day"]] = m["status"]
            continue
        so, sc = _first_interval(s["hours"])
        ro, rc = _first_interval(s["shown"])
        o, c = m["open"] or so or ro, m["close"] or sc or rc
        if o and c and c <= o and int(c[:2]) < 12:   # "open at 8, close at 1" -> 13:00
            c = f"{int(c[:2]) + 12:02d}{c[2:]}"
        if not (m["open"] or m["close"]):
            unclear = f"Which hours are you open on {s['weekday'][:3]} {s['dm']}?"
        elif o and c and TIME.match(o) and TIME.match(c) and o < c:
            found[s["day"]] = f"{o}-{c}"
        else:
            unclear = f"Which hours are you open on {s['weekday'][:3]} {s['dm']}?"
    return found, unclear
