#!/usr/bin/env python3
"""End-to-end demo of the five-step loop for one business and one day.

    python examples/quickstart.py                       # Kahvila Aurora, Christmas Eve 2026
    python examples/quickstart.py SE-D0008 2026-12-24   # any entity + date from evaluation/targets_2026.csv

Starts the local Yext mock in the background (with a throw-away state file), then runs
1 Detect → 2 Suggest → 3 Contact → 4 Decide → 5 Apply with the rule-based baseline. Standard library only.
"""
from __future__ import annotations

import csv
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
from datetime import date
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mock_api"))
sys.path.insert(0, os.path.join(ROOT, "examples"))
os.environ.setdefault("MOCK_QUIET", "1")

import server  # noqa: E402
from baseline import Baseline  # noqa: E402
from yext_client import YextClient, owner_response  # noqa: E402

WD = {"fi": ["maanantaina", "tiistaina", "keskiviikkona", "torstaina", "perjantaina", "lauantaina", "sunnuntaina"],
      "sv": ["måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"],
      "nb": ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"],
      "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]}
TXT = {
    "fi": {"hi": "Hei {name}!", "day": "{day} on {wd} {dm}", "closed": "kiinni", "open": "auki klo {h}",
           "LAST_YEAR": "Viime vuonna olitte {last}, mutta tälle vuodelle poikkeusaikoja ei ole vielä merkitty.",
           "SHOPPING_CENTRE": "{mall} on silloin {mall_h}, mutta teille ei ole merkitty poikkeusaikoja.",
           "PEERS": "Useimmat vastaavat yritykset alueellanne ovat silloin kiinni, eikä teille ole merkitty poikkeusaikoja.",
           "shows": "Siksi Google näyttää nyt: {shown}.", "sug": "Ehdotamme: {dm} {sug}",
           "btn": "[Hyväksy] [Muokkaa] [Ei kiitos]"},
    "sv": {"hi": "Hej {name}!", "day": "{day} infaller {wd} {dm}.", "closed": "stängt", "open": "öppet {h}",
           "LAST_YEAR": "Förra året hade ni {last}, men inga avvikande tider är inlagda i år.",
           "SHOPPING_CENTRE": "{mall} har då {mall_h}, men inga avvikande tider är inlagda för er.",
           "PEERS": "De flesta liknande företag i området har stängt då, och inga avvikande tider är inlagda för er.",
           "shows": "Därför visar Google nu: {shown}.", "sug": "Vi föreslår: {dm} {sug}",
           "btn": "[Godkänn] [Ändra] [Nej tack]"},
    "nb": {"hi": "Hei {name}!", "day": "{day} er {wd} {dm}", "closed": "stengt", "open": "åpent {h}",
           "LAST_YEAR": "I fjor hadde dere {last}, men ingen avvikende åpningstider er lagt inn i år.",
           "SHOPPING_CENTRE": "{mall} har da {mall_h}, men ingen avvikende tider er lagt inn for dere.",
           "PEERS": "De fleste lignende bedrifter i området har stengt da, og ingen avvikende tider er lagt inn for dere.",
           "shows": "Derfor viser Google nå: {shown}.", "sug": "Vi foreslår: {dm} {sug}",
           "btn": "[Godkjenn] [Endre] [Nei takk]"},
    "en": {"hi": "Hi {name}!", "day": "{day} is on {wd} {dm}.", "closed": "closed", "open": "open {h}",
           "LAST_YEAR": "Last year you were {last}, but no special hours are set for this year.",
           "SHOPPING_CENTRE": "{mall} is {mall_h} that day, but no special hours are set for you.",
           "PEERS": "Most similar businesses nearby are closed that day, and no special hours are set for you.",
           "shows": "So Google now shows: {shown}.", "sug": "We suggest: {dm} {sug}",
           "btn": "[Approve] [Edit] [No thanks]"},
}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def nice(h):
    def t(x):
        hh, mm = x.split(":")
        return str(int(hh)) + ("" if mm == "00" else "." + mm)
    return ", ".join("–".join(t(x) for x in iv.split("-")) for iv in h.split(","))


def state(h, lang):
    return TXT[lang]["closed"] if h == "closed" else TXT[lang]["open"].format(h=nice(h))


def eta(sec):
    return f"~{sec // 60} min" if sec < 3600 else (f"~{sec // 3600} h" if sec < 86400 else f"~{sec // 86400} d")


def day_key_for(bl, eid, country, day):
    with open(os.path.join(ROOT, "evaluation", "targets_2026.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["entity_id"] == eid and r["date"] == day.isoformat():
                return r["day_key"]
    return next((r["day_key"] for r in bl.cal if r["date"] == day.isoformat() and r["country"] == country), None)


def main():
    eid = sys.argv[1] if len(sys.argv) > 1 else "FI-D0001"
    day = date.fromisoformat(sys.argv[2] if len(sys.argv) > 2 else "2026-12-24")
    server.STATE_FILE = os.path.join(tempfile.mkdtemp(), "state.json")   # never touch mock_api/state.json
    server.STORE = server.Store()
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    base = f"http://127.0.0.1:{port}"
    try:
        run(eid, day, base)
    finally:
        srv.shutdown()


def run(eid, day, base):
    yx = YextClient(base_url=base, api_key="demo")
    bl = Baseline(2026)
    if eid not in bl.ents:
        sys.exit(f"Unknown entity {eid}")
    ent = yx.get_entity(eid)
    country = ent["meta"]["countryCode"]
    key = day_key_for(bl, eid, country, day)
    if key is None:
        sys.exit(f"{day} is not a target day for {eid}. Pick a row from evaluation/targets_2026.csv.")
    print(f"\n{ent['name']} ({eid}, {ent['address']['city']}) — {day} {key}\n" + "-" * 64)
    shown = bl.displayed(eid, day)
    act, sug, basis, ev = bl.explain(eid, day, key)
    print("1 DETECT   Google & co. currently show:", shown)
    if basis == "LAST_YEAR":
        print(f"           Last year ({ev[0]}) the owner entered: {ev[1]}")
    elif basis == "SHOPPING_CENTRE":
        print(f"           Shopping centre {ev} has published special hours for this day")
    elif basis == "PEERS":
        print(f"           {round(ev * 100)}% of similar businesses that entered this day were closed")
    elif basis == "CLOSED_SIGNALS":
        print(f"           Signals: {ev}")
    else:
        print("           No last-year entry, shopping centre or strong peer pattern found")
    print(f"2 SUGGEST  baseline → {act} {sug}")
    if act == "FLAG":
        print("           Route to a human – don't send a holiday-hours message to a business that may be gone.")
        return
    if act != "SUGGEST":
        print("           No message from the baseline. Reviews, Google edits, the pilot log, events or weather might")
        print("           still reveal a change – that is where a smarter system earns points.")
        return
    lang = ent["meta"]["language"] if ent["meta"]["language"] in TXT else "en"
    t = TXT[lang]
    day_name = next((r["name_local"] for r in bl.cal if r["date"] == day.isoformat() and r["country"] == country), key)
    dm = f"{day.day}/{day.month}" if lang == "sv" else f"{day.day}.{day.month}."
    reason = t[basis].format(last=state(ev[1], lang) if basis == "LAST_YEAR" else "",
                             mall=ev if basis == "SHOPPING_CENTRE" else "",
                             mall_h=state(sug, lang) if basis == "SHOPPING_CENTRE" else "")
    lines = [t["hi"].format(name=ent["name"]),
             f"{t['day'].format(day=day_name, wd=WD[lang][day.weekday()], dm=dm)} {reason} "
             f"{t['shows'].format(shown=state(shown, lang))}",
             t["sug"].format(dm=dm, sug=state(sug, lang)), t["btn"]]
    print("3 CONTACT  message:")
    for line in lines:
        print("           " + line)
    try:
        res = owner_response(eid, day.isoformat(), sug, base_url=base)
    except urllib.error.HTTPError:
        print("4 DECIDE   (owner simulator answers only for dev and demo businesses – try a FI-D… id)")
        return
    print(f"4 DECIDE   owner simulator → {res['decision']}"
          + (f" after {res['latency_minutes']} min, final hours {res['final_hours']}" if res["final_hours"] else ""))
    if res["decision"] not in ("ACCEPTED", "EDITED"):
        print("5 APPLY    nothing written – the owner has the last word.")
        return
    yx.set_holiday_hours(eid, day.isoformat(), res["final_hours"])
    after = [h for h in yx.get_entity(eid)["hours"]["holidayHours"] if h["date"] == day.isoformat()]
    live = [l for l in yx.listings([eid])["listings"] if l["status"] == "LIVE"]
    print("5 APPLY    written to Yext:", after)
    print("           confirmation for the owner – new hours visible on:")
    for l in sorted(live, key=lambda x: x["typicalUpdateSpeedSeconds"]):
        print(f"             {l['publisherName']:40s} {eta(l['typicalUpdateSpeedSeconds'])}")


if __name__ == "__main__":
    main()
