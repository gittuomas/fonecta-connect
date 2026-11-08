"""Special opening hours from the owner's own Instagram posts.

Owners often announce holiday hours on Instagram ("Joulun aukioloajat: jouluaattona 8-12, joulupäivänä
suljettu") but never update Google. Connect reads the owner's own posts, finds dates and hours, compares
them with what Google shows, and asks on WhatsApp:

    "We saw on your Instagram that you're closed on Christmas Day. Google still shows you open 7:30-18.
     Shall we update Google and 8 other places?"   [Yes, update] [No]

Nothing changes without the owner's yes (step 5 writes it, with UNDO).

Legal basis (why this is allowed): Connect reads only the business's OWN posts, through Meta's official
Instagram API, after the owner has connected their professional (business or creator) Instagram account
to Connect and agreed. Fonecta already asks customers to connect Google and Facebook the same way. No
scraping of public pages (against Meta's terms), no other accounts, no personal accounts. In production
this needs Meta app review for the permission, a line in Connect's privacy notice, and a way to
disconnect. Not legal advice: Fonecta's legal team should confirm before launch.

The challenge data has no social media, so the demo uses SAMPLE captions (marked as samples).
fetch_instagram() shows the production call; it is not tested against a live account.
Standard library only.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import date

# Fallback names only; the owner page uses the calendar, with the country's own name where the English one
# isn't familiar (Tapaninpäivä, Loppiainen).
DAY_NAME = {(12, 24): "Christmas Eve", (12, 25): "Christmas Day", (12, 31): "New Year's Eve", (1, 1): "New Year's Day",
            (12, 6): "Independence Day"}

# Holiday names in Finnish, Swedish, Norwegian and English -> (month, day)
HOLIDAYS = [
    (r"jouluaat\w*|julafton\w*|julaften\w*|christmas eve", (12, 24)),
    (r"joulupäiv\w*|juldag\w*|1\.\s*juledag\w*|første juledag\w*|christmas day", (12, 25)),
    (r"tapaninpäiv\w*|tapanina|annandag jul\w*|2\.\s*juledag\w*|andre juledag\w*|boxing day|st\.? stephen'?s day", (12, 26)),
    (r"uudenvuodenaat\w*|nyårsafton\w*|nyttårsaften\w*|new year'?s eve", (12, 31)),
    (r"uudenvuodenpäiv\w*|nyårsdag\w*|1\.\s*nyttårsdag\w*|første nyttårsdag\w*|new year'?s day", (1, 1)),
    (r"loppiai\w*|trettondedag jul\w*|trettende\w*|epiphany", (1, 6)),
    (r"itsenäisyyspäiv\w*|independence day", (12, 6)),
]
NUM_DATE = r"\b(\d{1,2})\.(\d{1,2})\.(?!\d)|\b(\d{1,2})/(\d{1,2})\b"      # 24.12.  24/12  (not times like 7.30)
MONTHS = {"jan": 1, "tammi": 1, "dec": 12, "joulu": 12}
NAME_DATE = r"\b(dec|jan)[a-z]*\.?\s+(\d{1,2})\b|\b(\d{1,2})\.?\s+(dec|jan)[a-z]*\b"           # Dec 24, 24 Dec
CLOSED = re.compile(r"suljettu|kiinni|lukossa|stängt|stängda|stengt|closed", re.I)
HOURS = re.compile(r"(\d{1,2})(?:[.:](\d{2}))?\s*(?:-|–|—|to)\s*(\d{1,2})(?:[.:](\d{2}))?")

# SAMPLE captions for the demo (the challenge data has no social media). Each is the owner's own post on
# their connected business account. In production these come from fetch_instagram().
SAMPLES = {
    "FI-D0001": [{"posted": "2026-11-04", "permalink": "https://www.instagram.com/p/sample-aurora/",
                  "caption": "Joulun aukioloajat ☕🎄 Jouluaattona 24.12. olemme auki klo 7.30–13 ja "
                             "joulupäivänä suljettu. Hyvää joulua kaikille!"}],
    "SE-D0008": [{"posted": "2026-11-03", "permalink": "https://www.instagram.com/p/sample-linnea/",
                  "caption": "Julöppettider 🎄 Vi har stängt på julafton och juldagen. "
                             "Trettondedag jul har vi öppet 10–16. God jul!"}],
    "NO-D0009": [{"posted": "2026-11-05", "permalink": "https://www.instagram.com/p/sample-fjord/",
                  "caption": "Åpningstider i jula 🥨 julaften 7–12, 1. juledag stengt, nyttårsaften 7–14. God jul!"}],
    "FI-D0003": [{"posted": "2026-11-02", "permalink": "https://www.instagram.com/p/sample-kide/",
                  "caption": "Uudet joulukimput ovat saapuneet! 💐 Tule katsomaan."}],   # no hours: stays quiet
    # Campaigns (no hours in them): Connect suggests posting the offer on Google and Facebook too.
    "FI-D0004": [{"posted": "2026-11-05", "permalink": "https://www.instagram.com/p/sample-lyhty/",
                  "caption": "BLACK FRIDAY 🖤 -30 % kaikista talvitakeista pe–su 27.–29.11. Tervetuloa!"}],
    "NO-D0015": [{"posted": "2026-11-04", "permalink": "https://www.instagram.com/p/sample-gym/",
                  "caption": "Tilbud i november 💪 50 % rabatt på første måned for nye medlemmer. Gjelder til 30.11."}],
}

# Words that mark a campaign or offer (fi / sv / nb / en), or a percentage discount.
CAMPAIGN = re.compile(r"tarjou|\bale\b|alennus|kampanja|black friday|cyber monday|\brea\b|erbjudande|kampanj|"
                      r"tilbud|rabatt|\bsale\b|\boffer|discount|-\s?\d{1,2}\s?%|\d{1,2}\s?%\s?(?:off|alennus|rabatt)", re.I)
CAMPAIGN_FRESH_DAYS = 30       # only recent posts


DISCOUNT = re.compile(r"-\s?(\d{1,2})\s?%|(\d{1,2})\s?%")
RANGE_FULL = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.?\s?[–-]\s?(\d{1,2})\.(\d{1,2})\.")   # 27.12.–31.12.
RANGE = re.compile(r"(?<![.\d])(\d{1,2})\.?\s?[–-]\s?(\d{1,2})\.(\d{1,2})\.")              # 27.–29.11.
UNTIL = re.compile(r"\basti\b|\bsaakka\b|\btill\b|\btil\b|\buntil\b|\bgjelder\b|\bgäller\b|\bvoimassa\b", re.I)
LABELS = [(r"black friday", "Black Friday"), (r"cyber monday", "Cyber Monday"),
          (r"jouluale|joulutarjou|julrea|julerbjud|juletilbud|christmas sale", "Christmas sale")]


def campaign_facts(caption: str, posted: date) -> dict:
    """Only the facts of an offer, never its wording: {"label", "discount", "start", "end"}.
    Missing facts are None; the owner can add the rest with "Edit text"."""
    label = next((name for pattern, name in LABELS if re.search(pattern, caption, re.I)), None)
    m = DISCOUNT.search(caption)
    discount = int(m.group(1) or m.group(2)) if m else None
    start = end = None
    f, r = RANGE_FULL.search(caption), RANGE.search(caption)
    if f:
        start = _next(int(f.group(2)), int(f.group(1)), posted)
        end = _next(int(f.group(4)), int(f.group(3)), posted)
    elif r:
        start, end = _next(int(r.group(3)), int(r.group(1)), posted), _next(int(r.group(3)), int(r.group(2)), posted)
    else:
        days = [_next(mo, d, posted) for _, (mo, d) in _dates(caption)]
        days = [d for d in days if d]
        if days and UNTIL.search(caption):
            end = max(days)
        elif days:
            start = end = days[0]
    return {"label": label, "discount": discount, "start": start, "end": end}


def campaigns(posts: list[dict], today: date) -> list[dict]:
    """The owner's own posts that announce a campaign that hasn't ended, newest first.
    A post with dates counts while its last date is today or later; one without dates while it's recent."""
    out = []
    for p in sorted(posts, key=lambda p: p["posted"], reverse=True):
        posted = date.fromisoformat(p["posted"])
        if not CAMPAIGN.search(p["caption"]) or (today - posted).days > CAMPAIGN_FRESH_DAYS:
            continue
        days = [_next(m, d, posted) for _, (m, d) in _dates(p["caption"])]
        days = [d for d in days if d]
        if days and max(days) < today:
            continue
        facts = campaign_facts(p["caption"], posted)
        out.append(dict(p, until=max(days) if days else None, facts=facts,
                        headline=f"{facts['label']} offer" if facts["label"] else "Special offer"))
    return out


def fetch_instagram(access_token: str, limit: int = 20) -> list[dict]:
    """PRODUCTION (not tested against a live account): the business's own recent posts through Meta's
    Instagram API, with the token the owner granted when connecting Instagram to Connect.
    Returns [{"posted", "permalink", "caption"}]."""
    q = urllib.parse.urlencode({"fields": "caption,timestamp,permalink", "limit": limit, "access_token": access_token})
    with urllib.request.urlopen(f"https://graph.instagram.com/me/media?{q}", timeout=10) as r:
        data = json.load(r).get("data", [])
    return [{"posted": d.get("timestamp", "")[:10], "permalink": d.get("permalink", ""), "caption": d.get("caption", "")}
            for d in data if d.get("caption")]


def _next(month: int, day: int, today: date) -> date | None:
    for year in (today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            return None
        if d >= today:
            return d
    return None


def _hours(m) -> str | None:
    o = f"{int(m.group(1)):02d}:{m.group(2) or '00'}"
    c = f"{int(m.group(3)):02d}:{m.group(4) or '00'}"
    return f"{o}-{c}" if o < c <= "24:00" else None


def _dates(clause: str) -> list[tuple[int, tuple[int, int]]]:
    """(position, (month, day)) for every date or holiday name in a clause."""
    out = []
    for pattern, md in HOLIDAYS:
        out += [(m.start(), md) for m in re.finditer(pattern, clause, re.I)]
    for m in re.finditer(NUM_DATE, clause):
        d, mo = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        if 1 <= int(mo) <= 12:
            out.append((m.start(), (int(mo), int(d))))
    for m in re.finditer(NAME_DATE, clause, re.I):
        mon, d = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        out.append((m.start(), (MONTHS[mon.lower()[:3]], int(d))))
    return sorted(out)


def read_caption(caption: str, today: date) -> dict[date, str]:
    """{date: 'closed' | 'HH:MM-HH:MM'} for the special days a caption announces.
    Split into sentences and comma parts; in each part, every day gets the hours (or "closed") that come
    right after it, or else the nearest ones before it ("closed on Christmas Day and Boxing Day")."""
    found: dict[date, str] = {}
    for clause in re.split(r"[,\n;!]|(?<=[.?])\s+(?=[A-ZÅÄÖ])", caption):
        days = _dates(clause)
        if not days:
            continue
        blank = clause                                   # hide dates so "24.12" isn't read as hours
        for pattern in [p for p, _ in HOLIDAYS] + [NUM_DATE, NAME_DATE]:
            blank = re.sub(pattern, lambda m: " " * len(m.group()), blank, flags=re.I)
        states = [(m.start(), "closed") for m in CLOSED.finditer(blank)]
        states += [(m.start(), _hours(m)) for m in HOURS.finditer(blank)]
        states = sorted((pos, st) for pos, st in states if st)
        if not states:
            continue
        seen = set()
        for i, (pos, md) in enumerate(days):
            if md in seen:
                continue
            seen.add(md)
            nxt = days[i + 1][0] if i + 1 < len(days) else len(clause)
            after = [st for p, st in states if pos < p < nxt]
            before = [st for p, st in states if p < pos]
            st = after[0] if after else (before[-1] if before else None)
            # a day named right before another day with no hours between: "julafton och juldagen ... stängt"
            if not after and i + 1 < len(days):
                later = [st2 for p, st2 in states if p > pos]
                st = before[-1] if before else (later[0] if later else None)
            d = _next(*md, today)
            if d and st:
                found[d] = st
    return found


def check(posts: list[dict], shown, today: date) -> list[dict]:
    """Days where the owner's Instagram and Google disagree.
    shown(day) -> what Google shows now ('closed' / 'HH:MM-HH:MM'). Newest post wins for a day."""
    latest: dict[date, tuple[str, dict]] = {}
    for p in sorted(posts, key=lambda p: p["posted"]):
        for d, hrs in read_caption(p["caption"], today).items():
            latest[d] = (hrs, p)
    out = []
    for d in sorted(latest):
        hrs, p = latest[d]
        now = shown(d)
        if now != hrs:
            out.append({"day": d, "instagram": hrs, "google": now, "post": p})
    return out
