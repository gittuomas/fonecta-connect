"""Turn suggestion objects into call bundles: one owner, up to four dates, one reason each.

Source of suggestions, in order:
1. The suggestion engine's output file (handoff section 8 schema) at config.SUGGESTIONS_PATH.
2. Fallback until the engine lands: the package baseline's explain() for the demo businesses.
   It reads only data/ (no labels, no sim_data.json), so it is the same honest signal the
   engine starts from.

The team's Yext sandbox business has no history in the dataset, so it borrows the suggestions
of SANDBOX_STANDIN_FOR (Kahvila Aurora by default) while its "shown" hours are read live from
the sandbox. That keeps the Detect step real for the business we actually write to.
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import date, datetime
from functools import lru_cache

from . import config, i18n
from .hours import pretty

# Search views at risk per day type on dev 2026 (data guide, section 3); used as a priority prior
VIEW_WEIGHT = {"XMAS_DAY": 12.5, "XMAS_EVE": 10.6, "NEW_YEARS_DAY": 10.2, "BOXING_DAY": 8.1, "NYE": 7.9,
               "EPIPHANY": 7.5, "EPIPHANY_EVE": 5.0, "INTERDAY": 4.1, "DEC23": 3.1, "BLACK_FRIDAY": 2.0,
               "INDEPENDENCE_DAY": 2.0, "FATHERS_DAY": 1.0, "LUCIA": 0.5, "EVENT": 1.5, "SCHOOL_BREAK": 1.5}
# Exact precision of each baseline rule on dev 2026 (data guide, section 5): a stand-in confidence
BASIS_CONFIDENCE = {"LAST_YEAR": 0.93, "SHOPPING_CENTRE": 0.84, "PEERS": 0.87}

DEMO_ENTITIES = ["FI-D0001", "FI-D0002", "FI-D0004", "FI-D0007", "SE-D0008", "NO-D0009", "FI-D0010",
                 "FI-D0011", "FI-D0012", "FI-D0013", "FI-D0017", "SE-D0016"]
SANDBOX_BUNDLE = "SANDBOX"


def _pkg(*p):
    return config.PKG_DIR.joinpath(*p)


@lru_cache(maxsize=1)
def entities() -> dict:
    data = json.loads(_pkg("data", "entities.json").read_text(encoding="utf-8"))["response"]["entities"]
    return {e["meta"]["id"]: e for e in data}


@lru_cache(maxsize=1)
def contacts() -> dict:
    with _pkg("data", "customer_contacts.csv").open(encoding="utf-8") as f:
        return {r["entity_id"]: r for r in csv.DictReader(f)}


@lru_cache(maxsize=1)
def targets() -> dict:
    out: dict[str, list] = {}
    with _pkg("evaluation", "targets_2026.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.setdefault(r["entity_id"], []).append(r)
    return out


@lru_cache(maxsize=1)
def baseline():
    sys.path.insert(0, str(_pkg("examples")))
    from baseline import Baseline  # noqa: E402  (package code, MIT, used unmodified)
    return Baseline(2026)


def _state(h: str) -> str:
    return "closed" if h == "closed" else f"open {pretty(h)}"


def _reason_en(basis: str, ev, shown: str, proposed: str, weekday_en: str) -> str:
    if basis == "LAST_YEAR":
        last_day, last = ev
        d = date.fromisoformat(last_day)
        last_txt = "on regular hours" if last == "REGULAR" else _state(last)
        return (f"Last year on {i18n.date_label(d, 'en')} {d.year} they were {last_txt}, but nothing is entered "
                f"for this year, so the listing shows their regular {weekday_en} hours ({_state(shown)}).")
    if basis == "SHOPPING_CENTRE":
        return (f"Their shopping centre, {ev}, has published {_state(proposed)} for that day, but their own "
                f"listing still shows {_state(shown)}.")
    if basis == "PEERS":
        return (f"About {round(float(ev) * 100)}% of similar businesses nearby that entered this day are closed, "
                f"and they have no history of their own yet, so the listing shows {_state(shown)}.")
    return str(ev or "")


def _reason_short(basis: str, ev, proposed: str) -> str:
    """The reason in two to five words, said as the tail of the question: "Closed, like last year?"."""
    if basis == "LAST_YEAR":
        return "like last year"
    if basis == "SHOPPING_CENTRE":
        return f"like {ev}"
    if basis == "PEERS":
        return "like most similar places nearby"
    return ""


def _engine_suggestions() -> list[dict] | None:
    p = config.SUGGESTIONS_PATH
    if not p.exists():
        return None
    rows = json.loads(p.read_text(encoding="utf-8"))
    return rows if isinstance(rows, list) else rows.get("suggestions", [])


def suggestions_for(entity_id: str) -> list[dict]:
    """Suggestion objects (handoff schema subset) for one dataset business."""
    eng = _engine_suggestions()
    if eng is not None:
        return [s for s in eng if s.get("entity_id") == entity_id]
    bl = baseline()
    ent = entities()[entity_id]
    out = []
    for r in targets().get(entity_id, []):
        d = date.fromisoformat(r["date"])
        action, hours, basis, ev = bl.explain(entity_id, d, r["day_key"])
        if action == "NO_ACTION":
            continue
        shown = bl.displayed(entity_id, d)
        wd = i18n.WEEKDAY["en"][d.weekday()]
        conf = 1.0 if action == "FLAG" else BASIS_CONFIDENCE.get(basis, 0.6)
        out.append({
            "suggestion_id": f"{entity_id}|{r['date']}", "entity_id": entity_id, "date": r["date"],
            "day_key": r["day_key"], "action": action, "shown_hours": shown, "proposed_hours": hours,
            "confidence": conf,
            "priority": round(VIEW_WEIGHT.get(r["day_key"], 1.0) * conf *
                              (1.5 if hours == "closed" and shown != "closed" else 1.0), 2),
            "evidence": [{"source": basis or "NONE",
                          "detail": ev if action == "FLAG" else _reason_en(basis, ev, shown, hours, wd)}],
            "language": ent["meta"]["language"],
            "_basis": [basis, ev] if action == "SUGGEST" else None,
        })
    return out


def _owner(entity_id: str) -> dict:
    c = contacts().get(entity_id, {})
    return {"owner_first_name": c.get("contact_first_name", ""), "language": c.get("preferred_language", "en"),
            "preferred_channel": c.get("preferred_channel", "SMS"), "mobile": c.get("mobile", ""),
            "best_contact_window": c.get("best_contact_window", "10-12"),
            "standing_permission_holidays": c.get("standing_permission_holidays") == "1",
            "opted_out": c.get("opted_out_of_suggestions") == "1"}


def _items(sugs: list[dict], lang: str, country: str, now: datetime, shown_override=None) -> tuple[list, list]:
    horizon = (now.date().toordinal() + config.VOICE_HORIZON_DAYS)
    flags = [s for s in sugs if s["action"] == "FLAG"]
    cand = []
    for s in sugs:
        d = date.fromisoformat(s["date"])
        if s["action"] != "SUGGEST" or d <= now.date() or d.toordinal() > horizon:
            continue
        shown = shown_override(d) if shown_override else s["shown_hours"]
        if shown == s["proposed_hours"]:
            continue                                   # already right, stay quiet
        s = {**s, "shown_hours": shown}
        if shown_override and s.get("_basis"):         # re-word the reason around what this listing shows
            basis, ev = s["_basis"]
            s["evidence"] = [{"source": basis, "detail": _reason_en(basis, ev, shown, s["proposed_hours"],
                                                                     i18n.WEEKDAY["en"][d.weekday()])}]
        cand.append(s)
    cand = sorted(cand, key=lambda s: -s.get("priority", 0))[:config.VOICE_MAX_ITEMS]
    cand.sort(key=lambda s: s["date"])
    items = []
    for n, s in enumerate(cand, 1):
        d = date.fromisoformat(s["date"])
        ev = s.get("evidence") or []
        items.append({
            "item": n, "suggestion_id": s["suggestion_id"], "date": s["date"], "day_key": s["day_key"],
            "day_name": i18n.day_name(s["day_key"], lang), "day_name_en": i18n.day_name(s["day_key"], "en"),
            "date_label": i18n.date_label(d, lang), "date_label_en": i18n.date_label(d, "en"),
            "shown_hours": s["shown_hours"], "proposed_hours": s["proposed_hours"],
            "confidence": s.get("confidence", 0), "priority": s.get("priority", 0),
            "reason_en": ev[0]["detail"] if ev else "", "evidence": ev,
            "reason_short": (_reason_short(*s["_basis"], s["proposed_hours"]) if s.get("_basis")
                             else (ev[0]["detail"] if ev else "")),
        })
    return items, flags


def build_bundle(entity_id: str, now: datetime, live_shown=None) -> dict:
    """Bundle for a dataset business, or for the sandbox business when entity_id == SANDBOX_BUNDLE."""
    sandbox = entity_id == SANDBOX_BUNDLE
    source_id = config.SANDBOX_STANDIN_FOR if sandbox else entity_id
    ent = entities()[source_id]
    owner = _owner(source_id)
    if sandbox:
        owner.update(owner_first_name=config.DEMO_OWNER_NAME, language=config.DEMO_OWNER_LANGUAGE,
                     mobile=config.DEMO_OWNER_PHONE, preferred_channel="WHATSAPP",
                     standing_permission_holidays=False, opted_out=False)
    lang = config.DEMO_FORCE_LANGUAGE or owner["language"]
    lang = lang if lang in i18n.LANGS else "en"
    country = config.SANDBOX_COUNTRY if sandbox else ent["meta"]["countryCode"]
    items, flags = _items(suggestions_for(source_id), lang, country, now, live_shown if sandbox else None)
    name = config.YEXT_SANDBOX_ENTITY_ID if sandbox else ent["name"]
    if sandbox:                                   # own ids, so its decisions never hide the stand-in's
        for it in items:
            it["suggestion_id"] = f"{config.YEXT_SANDBOX_ENTITY_ID}|{it['date']}"
    headline = i18n.join([it["day_name"] for it in items], lang)
    brand = i18n.BRAND.get(country, "Connect")
    return {
        "bundle_id": f"{entity_id}|2026-27",
        "entity_id": entity_id, "write_entity_id": config.YEXT_SANDBOX_ENTITY_ID if sandbox else entity_id,
        "suggestions_from": source_id, "business_name": name,
        "city": config.SANDBOX_CITY if sandbox else ent["address"]["city"], "country": country,
        "timezone": i18n.TZ.get(country, "Europe/Helsinki"), "brand": brand,
        "segment": ent.get("c_segment", ""), **owner, "language": lang,
        "items": items, "flags": flags, "headline": headline,
        "greeting": i18n.greeting(lang, name=owner["owner_first_name"], brand=brand, business=name,
                                  headline=headline),
        "voicemail_message": i18n.voicemail(lang, name=owner["owner_first_name"], brand=brand, business=name),
    }


_NUM = ["twelve", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven"]
_MIN = {0: "", 15: " fifteen", 30: " thirty", 45: " forty-five"}


def _say_time(t: str) -> str:
    h, m = (int(x) for x in t.split(":"))
    if h in (0, 24) and m == 0:
        return "midnight"
    return _NUM[h % 12] + _MIN.get(m, f" {m:02d}")


def spoken(hours: str) -> str:
    """'07:30-13:00' -> 'seven thirty to one', 'closed' -> 'closed' (English, as said on the phone)."""
    if hours == "closed":
        return "closed"
    parts = []
    for iv in hours.split(","):
        a, b = iv.strip().split("-")
        parts.append("open all day" if (a, b) in (("00:00", "23:59"), ("00:00", "24:00")) else
                     f"{_say_time(a)} to {_say_time(b)}")
    return " and ".join(parts)


WELL_KNOWN = {"XMAS_EVE", "XMAS_DAY", "BOXING_DAY", "NYE", "NEW_YEARS_DAY", "INDEPENDENCE_DAY", "EPIPHANY",
              "FATHERS_DAY", "LUCIA"}


def brief(bundle: dict) -> str:
    """Facts and pronunciation cues for a concise conversation, without scripted questions.

    Well-known holidays can be said by name; other days retain their date. The prompt
    controls wording while each item keeps its exact proposed and currently shown hours."""
    lines = []
    for it in bundle["items"]:
        name = it["day_name_en"] + (f" ({it['day_name']})" if it["day_name"] != it["day_name_en"] else "")
        say_name = it["day_name_en"] if it["day_key"] in WELL_KNOWN else f"{it['day_name_en']}, {it['date_label_en']}"
        lines.append(
            f"{it['item']}. {name}, {it['date_label_en']}. Google now: {_state(it['shown_hours'])}. "
            f"Suggest: {_state(it['proposed_hours'])}. Why: {it['reason_en']} "
            f"Say date: {say_name}. Spoken proposal: {spoken(it['proposed_hours'])}.")
    return "\n".join(lines)
