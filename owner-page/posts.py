"""Post suggestions on WhatsApp: a preview with picture, text and buttons, no login.

Today Fonecta writes one post a month for each customer (from the customer's website) and publishes it on
their Google Business Profile and Facebook. The owner hears about it by email, and has to log in to change
or stop it (Fonecta Q&A, 7 Nov 2026). Here the same post arrives on WhatsApp instead, plus two new kinds:

    holiday  right after the owner confirms holiday hours: "Shall we post your Christmas hours?"
             Built only from the hours they just confirmed.                     -> needs a yes
    event    a big event near the business: a ready post with the event, the distance and the
             business's own hours that day. Never says the business takes part.   -> needs a yes
             (Fonecta: event posts need a clearer yes than the monthly post)
    seasonal a question first, so nothing is made up: "Do you have a Christmas menu this year? Tell us
             and we'll make a post." The owner's own words become the post.          -> needs a yes
    campaign the owner announced an offer on their own Instagram ("-30 % on winter coats"): "Shall we also
             post it on Google and Facebook?" Written from the FACTS only (kind of offer, discount, dates);
             the owner's post itself is never copied. Needs a yes.
    monthly  Fonecta's existing monthly post, previewed in WhatsApp.            -> goes live after 7 days
             with a reminder the day before (Fonecta's current rule; posts are low-risk).

Buttons: Yes, post it / Edit text / No thanks (monthly: Publish now / Edit text / Skip this one).
"Edit text": the owner sends the text they want and gets a new preview; nothing goes live meanwhile.
After publishing, replying DELETE takes the post down.

Pictures (team decision): only the business's OWN photos from its Connect profile, never event photos.
Event photos are copyrighted: Helsinki's event API marks many "event_only" (only for presenting the event),
Ticketmaster's terms cover promoting the event itself, and even freely licensed (CC BY) photos would make it
look like the business is part of the event. The challenge data has no business photos, so the demo shows a
simple card in place of the owner's photo.

Publishing is simulated: the challenge's Yext mock has no posts endpoint. In production this would go
through Connect's existing post publishing. Standard library only; app.py supplies the business data.
"""
from __future__ import annotations

import html
import math
from datetime import date, timedelta

PUBLISH_TO = "Google and Facebook"
MONTHLY_GO_LIVE_DAYS = 7
DELETE_WORDS = {"delete", "remove", "take it down", "poista"}
BUTTONS = {
    "ask": [("yes", "Yes, post it"), ("edit", "Edit text"), ("no", "No thanks")],         # max 20 characters
    "monthly": [("yes", "Publish now"), ("edit", "Edit text"), ("no", "Skip this one")],
    "question": [("yes", "Yes, I'll tell you"), ("no", "Not this year")],
}
COLORS = {"holiday": ("#0b6e4f", "#e8f5ef"), "event": ("#5b2a86", "#f1eaf8"), "monthly": ("#14198a", "#eceefe"),
          "seasonal": ("#9b1c1c", "#fbecec"), "campaign": ("#a14400", "#fdf0e6")}
ICON = {"cafe": "☕", "bakery": "🥐", "restaurant": "🍽️", "lunch_restaurant": "🍽️", "pizzeria": "🍕", "bar_pub": "🍺",
        "hair_salon": "✂️", "beauty_salon": "💅", "florist": "💐", "gym": "🏋️", "clothing_store": "👗",
        "dental_clinic": "🦷", "accounting_office": "📊", "indoor_playground": "🧸", "seasonal_terrace": "☀️",
        "ski_rental": "⛷️"}

POSTS: dict[str, dict] = {}     # post id -> post
PUBLISHED: list[dict] = []      # simulated publish log (what would go to Google and Facebook)


def h(s):
    return html.escape(str(s))


def day_label(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


# ---------------------------------------------------------------- text, only from the business's own data

def holiday_text(name: str, title: str, days: list[tuple[date, str, str]]) -> tuple[str, str]:
    """(headline, text). days = [(date, day name, hours label)] the owner just confirmed."""
    wish = {"Christmas": "Merry Christmas!", "New Year": "Happy New Year!"}.get(title, "")
    if len(days) == 1:
        d, day_name, hrs = days[0]
        text = f"{day_name}, {day_label(d)}: {name} is {hrs}."
        return f"{day_name} hours", (text + (f" {wish}" if wish else "")).strip()
    lines = "\n".join(f"{day_label(d)}: {hrs}" for d, _, hrs in days)
    return f"{title} hours", f"{title} opening hours at {name}:\n{lines}" + (f"\n\n{wish}" if wish else "")


def event_text(name: str, event: str, venue: str, when: str, crowd: int | None, metres: int | None,
               hours: str) -> tuple[str, str]:
    near = f"about {metres:,} m from {venue}" if metres is not None and metres < 2000 else f"near {venue}"
    crowd_txt = f" About {crowd:,} visitors are expected." if crowd else ""
    return (f"{event} nearby",
            f"{event} at {venue}, {when}.{crowd_txt}\n\nComing to the area? {name} is {near}, "
            f"and we're {hours} that day. See you then!")   # not "see you there": the business isn't at the event


def campaign_text(name: str, facts: dict) -> tuple[str, str]:
    """(headline, text) from the facts of an offer only: 'Black Friday at Muoti Lyhty: 30% off, Fri 27 Nov – Sun 29 Nov.'"""
    label = facts.get("label") or "Special offer"
    text = f"{label} at {name}" + (f": {facts['discount']}% off" if facts.get("discount") else "")
    start, end = facts.get("start"), facts.get("end")
    if start and end and start != end:
        text += f", {day_label(start)} – {day_label(end)}"
    elif start:
        text += f" on {day_label(start)}"
    elif end:
        text += f", until {day_label(end)}"
    return f"{label} offer" if facts.get("label") else "Special offer", text + ". Welcome!"


def monthly_text(name: str, city: str, week: str) -> tuple[str, str]:
    return (f"Welcome to {name}",
            f"Welcome to {name} in {city}! Our opening hours:\n{week}\n\nSee you soon!")


def metres_between(a: dict | None, b: list | None) -> int | None:
    """a = {'latitude', 'longitude'} (Yext), b = [lon, lat] (event GeoJSON). Rounded to 50 m."""
    if not a or not b:
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, (a["latitude"], a["longitude"], b[1], b[0]))
    x = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return int(round(6371000 * 2 * math.asin(math.sqrt(x)) / 50) * 50)


# ---------------------------------------------------------------- which events are relevant to which business
FOOD = {"cafe", "bakery", "restaurant", "lunch_restaurant", "pizzeria", "bar_pub", "hotel"}
SHOPS = {"florist", "clothing_store", "bookstore", "grocery", "pet_store", "hardware_store", "optician", "pharmacy"}
STROLL = {"christmas_market", "city_festival"}       # events where visitors walk around and drop into shops
MAX_METRES = 1000           # walking distance
MIN_CROWD = 2000            # big enough to matter
UNKNOWN_CROWD_METRES = 500  # live feeds often don't give a crowd size: then only very close events
EVENT_DAYS = 35             # events up to 5 weeks ahead; the post suggestion goes out about 2 weeks before
SEND_DAYS_BEFORE = 14


def _mins(hhmm: str) -> int:
    hh, mm = hhmm.split(":")
    return int(hh) * 60 + int(mm)


def _open_during(hours: str, start: int, end: int) -> bool:
    """Is the business open at any time in [start, end] (minutes from midnight; may run past midnight)?"""
    if not hours or hours == "closed":
        return False
    for iv in hours.split(","):
        o, c = (_mins(x) for x in iv.split("-"))
        if c <= o:
            c += 24 * 60
        if o < end and c > start:
            return True
    return False


def event_relevance(segment: str, keywords: list, metres: int | None, crowd: int | None, hours: str,
                    start: str, end: str) -> tuple[bool, str]:
    """(relevant?, short reason). start/end 'HH:MM'. Every rule has to pass."""
    kinds = set(keywords or [])
    if segment in FOOD:
        why = "visitors eat and drink before and after"
        lo, hi = _mins(start) - 60, _mins(end) + 120
    elif segment in SHOPS and kinds & STROLL:
        why = "visitors stroll around and drop into shops"
        lo, hi = _mins(start), _mins(end)
    elif segment in SHOPS:
        return False, "visitors go to this kind of event and leave, they don't shop around"
    else:
        return False, "not a business people drop into around an event"
    if hi < lo:
        hi += 24 * 60
    if metres is None or metres > MAX_METRES:
        return False, "too far to walk"
    if crowd is None and metres > UNKNOWN_CROWD_METRES:
        return False, "crowd size unknown and not right next door"
    if crowd is not None and crowd < MIN_CROWD:
        return False, f"small event ({crowd:,} visitors)"
    if not _open_during(hours, lo, hi):
        return False, "closed while visitors are around"
    return True, why


# ---------------------------------------------------------------- seasonal ideas: ask first, then post
# The owner's own words become the post, so nothing is invented. Asked once per season, 1 Nov - 15 Dec.
SEASON = (11, 1), (12, 15)
SEASONAL = {
    "dining": ({"restaurant", "lunch_restaurant", "hotel", "pizzeria"}, "Christmas menu",
               "Do you have a Christmas menu or Christmas party (pikkujoulu) offers this year?",
               "Christmas menu 23.11.–23.12., 4 courses, 59 €, glögi included",
               "{text}\n\nBook a table: {phone}"),
    "parties": ({"bar_pub"}, "Christmas parties",
                "Are you taking bookings for Christmas parties (pikkujoulut) this year?",
                "Christmas party packages for groups of 10–40, Fridays and Saturdays in December",
                "{text}\n\nBookings: {phone}"),
    "treats": ({"cafe", "bakery"}, "Christmas treats",
               "Are you baking Christmas treats this year, like joulutortut or gingerbread?",
               "Joulutortut and gingerbread from 23 November, Christmas cakes to order",
               "{text}\n\nSee you soon!"),
    "flowers": ({"florist"}, "Christmas flowers",
                "Do you have Christmas flowers or wreaths coming in?",
                "Hyacinths, amaryllis and door wreaths from 1 December",
                "{text}\n\nWelcome!"),
    "appointments": ({"hair_salon", "beauty_salon", "massage"}, "Book before Christmas",
                     "Do you still have free appointments before Christmas?",
                     "A few free times left in the week before Christmas",
                     "{text}\n\nBook: {phone}"),
    "gifts": ({"clothing_store", "bookstore", "pet_store"}, "Christmas gift ideas",
              "Do you have Christmas gift ideas you'd like to show your customers?",
              "Gift cards and new winter scarves, gift wrapping free",
              "{text}\n\nWelcome!"),
}


def seasonal_idea(segment: str, today: date):
    """(key, headline, question, example, template) for this kind of business in the Christmas season, or None."""
    if not ((today.month, today.day) >= SEASON[0] and (today.month, today.day) <= SEASON[1]):
        return None
    for key, (segs, head, question, example, template) in SEASONAL.items():
        if segment in segs:
            return key, head, question, example, template
    return None


# ---------------------------------------------------------------- posts and what the owner does with them

def make(pid: str, eid: str, kind: str, headline: str, text: str, segment: str, intro: str,
         sent: date, title: str = "") -> dict:
    """Create the post once (later calls return the same one)."""
    if pid not in POSTS:
        POSTS[pid] = {"id": pid, "eid": eid, "kind": kind, "headline": headline, "text": text, "segment": segment,
                      "first_text": text, "intro": intro, "title": title, "status": "suggested", "chat": [], "sent": sent,
                      "today": sent, "reminded": False, "template": "", "fill": {},
                      "go_live": sent + timedelta(days=MONTHLY_GO_LIVE_DAYS) if kind == "monthly" else None}
    return POSTS[pid]


def make_seasonal(pid: str, eid: str, headline: str, question: str, example: str, template: str, fill: dict,
                  segment: str, sent: date) -> dict:
    """A seasonal idea: first a question, then a post from the owner's own answer."""
    if pid not in POSTS:
        p = make(pid, eid, "seasonal", headline, "", segment, question, sent)
        p.update(status="asked", template=template, fill=fill, example=example)
    return POSTS[pid]


def publish(post: dict, by: str) -> None:
    post["status"] = "published"
    PUBLISHED.append({"post": post["id"], "eid": post["eid"], "to": PUBLISH_TO, "by": by, "on": str(post["today"]),
                      "text": post["text"]})


def act(post: dict, a: str) -> None:
    """A quick-reply button: yes / edit / no. Counts only while the post is waiting for an answer."""
    if post["status"] == "asked":                     # seasonal idea: "Yes, I'll tell you" / "Not this year"
        if a not in ("yes", "no"):
            return
        post["chat"].append(("me", dict(BUTTONS["question"])[a]))
        if a == "yes":
            post["status"] = "details"
            post["chat"].append(("them", f"Great! Send the details in one message, for example:\n\n"
                                         f"\"{post['example']}\"\n\nI'll show you the post before anything goes live."))
        else:
            post["status"] = "skipped"
            post["chat"].append(("them", "OK, no problem. We won't ask again this season."))
        return
    if post["status"] not in ("suggested", "editing") or a not in ("yes", "edit", "no"):
        return
    label = dict(BUTTONS["monthly" if post["kind"] == "monthly" else "ask"])[a]
    post["chat"].append(("me", label))
    if a == "yes":
        publish(post, by="owner")
        post["chat"].append(("them", f"Published ✅ Your post is now live on {PUBLISH_TO}.\n\n"
                                     "Want it gone? Reply DELETE and we'll take it down."))
    elif a == "edit":
        post["status"] = "editing"
        post["chat"].append(("them", "Sure! Send the text you'd like, and I'll show you the post again. "
                                     "Nothing goes live until you're happy with it."))
    else:
        post["status"] = "skipped"
        post["chat"].append(("them", "OK, we'll skip this month's post. Nothing goes live." if post["kind"] == "monthly"
                                     else "OK, no post this time."))


def reply(post: dict, text: str) -> None:
    """A typed reply: the new text after "Edit text", or DELETE after publishing."""
    text = text.strip()
    if not text:
        return
    post["chat"].append(("me", text))
    if text.lower().strip(" .!") in DELETE_WORDS:
        if post["status"] == "published":
            post["status"] = "deleted"
            PUBLISHED.append({"post": post["id"], "eid": post["eid"], "to": PUBLISH_TO, "by": "owner",
                              "on": str(post["today"]), "text": "(taken down)"})
            post["chat"].append(("them", f"Done, the post is taken down from {PUBLISH_TO}."))
        else:
            post["status"] = "skipped"
            post["chat"].append(("them", "OK, nothing will be posted."))
        return
    if post["status"] == "details":                   # the owner's own words become the post
        body = post["template"].format(text=text, **post["fill"])
        post["text"], post["status"] = body, "suggested"
        post["chat"].append(("them", "Here's your post:"))
        post["chat"].append(("preview", body))
        return
    if post["status"] in ("suggested", "editing"):
        post["text"], post["status"] = text, "suggested"
        post["chat"].append(("them", "Here's your updated post:"))
        post["chat"].append(("preview", text))
        return
    post["chat"].append(("them", "Thanks! If you'd like a new post, just say so here."))


def tick(post: dict, days: int) -> None:
    """Demo clock for the monthly post: move time forward. Reminder the day before; live on day 7 if no answer.
    Tapping "Edit text" pauses it: the owner has acted, so nothing goes live without them."""
    if post["kind"] != "monthly":
        return
    post["today"] += timedelta(days=days)
    if post["status"] != "suggested":
        return
    if not post["reminded"] and post["today"] >= post["go_live"] - timedelta(days=1):
        post["reminded"] = True
        post["chat"].append(("them", f"Reminder: your post goes live tomorrow ({day_label(post['go_live'])}) on "
                                     f"{PUBLISH_TO}. Tap a button above to publish it now, edit it or skip it."))
    if post["today"] >= post["go_live"]:
        publish(post, by="no reply in 7 days")
        post["chat"].append(("them", f"Your post is now live on {PUBLISH_TO}, as planned.\n\n"
                                     "Want it gone? Reply DELETE and we'll take it down."))


def wants_text(post: dict | None, text: str) -> bool:
    """Should a typed reply in a shared chat go to this post (edit text, or DELETE after publishing)?"""
    if not post:
        return False
    return post["status"] in ("editing", "details") or (post["status"] == "published"
                                           and text.lower().strip(" .!") in DELETE_WORDS)


def status_text(post: dict) -> str:
    return {"suggested": (f"waiting · goes live {day_label(post['go_live'])} if no reply" if post["kind"] == "monthly"
                          else "waiting for a yes"),
            "editing": "owner is editing (paused)", "published": "published", "skipped": "skipped",
            "asked": "question sent", "details": "owner is writing the details",
            "deleted": "taken down"}[post["status"]]


# ---------------------------------------------------------------- WhatsApp look (classes from app.WA_CSS)

def image_svg(post: dict, business: str) -> str:
    """Stand-in for the business's own photo from its Connect profile (the challenge data has no photos).
    Never an event photo: event posts use the same business picture as any other post."""
    fg, bg = COLORS[post["kind"]]
    icon = {"holiday": "🎄" if post["title"] == "Christmas" else "🎆" if post["title"] == "New Year" else "📅"
            }.get(post["kind"], ICON.get(post["segment"], "📍"))
    return (f"<svg viewBox='0 0 400 210' role='img' aria-label='{h(post['headline'])}' "
            f"style='display:block;width:100%;border-radius:8px;margin-bottom:6px'>"
            f"<rect width='400' height='210' fill='{bg}'/><rect y='160' width='400' height='50' fill='{fg}'/>"
            f"<text x='200' y='95' font-size='64' text-anchor='middle'>{icon}</text>"
            f"<text x='200' y='145' font-size='22' font-weight='700' fill='{fg}' text-anchor='middle' "
            f"font-family='-apple-system,Segoe UI,Roboto,sans-serif'>{h(post['headline'])}</text>"
            f"<text x='200' y='192' font-size='17' fill='#fff' text-anchor='middle' "
            f"font-family='-apple-system,Segoe UI,Roboto,sans-serif'>{h(business)}</text>"
            f"<text x='392' y='18' font-size='10' fill='{fg}' text-anchor='end' opacity='.7' "
            f"font-family='-apple-system,Segoe UI,Roboto,sans-serif'>Your photo from Connect goes here</text></svg>")


def chat_html(post: dict, business: str, action: str) -> str:
    """The preview message (picture + text + buttons) and everything said after it. `action` = button URL."""
    if post["kind"] == "monthly":
        footer = f"Goes live on {day_label(post['go_live'])} unless you skip it."
    else:
        footer = "Nothing is posted without your yes."
    live = post["status"] == "suggested"
    previews = sum(1 for who, _ in post["chat"] if who == "preview")

    def preview(last: bool, intro: str, text: str) -> str:
        on = live and last
        btns = "".join(f"<button class='qb' name='a' value='{k}' {'' if on else 'disabled'}>"
                       f"<span aria-hidden='true'>↩</span> {h(label)}</button>"
                       for k, label in BUTTONS["monthly" if post["kind"] == "monthly" else "ask"])
        return (f"<form method='post' action='{h(action)}' class='tpl'><div class='b'>{image_svg(post, business)}"
                f"{h(intro) + chr(10) + chr(10) if intro else ''}<i>{h(text)}</i>"
                f"<span class='ft'>{h(footer)}</span><span class='t'>10:00</span></div>{btns}</form>")
    if post["kind"] == "seasonal":
        on = post["status"] == "asked"
        btns = "".join(f"<button class='qb' name='a' value='{k}' {'' if on else 'disabled'}>"
                       f"<span aria-hidden='true'>↩</span> {h(label)}</button>" for k, label in BUTTONS["question"])
        out = (f"<form method='post' action='{h(action)}' class='tpl'><div class='b'>{h(post['intro'])}"
               f"<span class='ft'>You'll see the post before anything goes live.</span><span class='t'>10:00</span>"
               f"</div>{btns}</form>")
    else:
        out = preview(previews == 0, post["intro"], post["first_text"])
    seen = 0
    for who, text in post["chat"]:
        if who == "preview":
            seen += 1
            out += preview(seen == previews, "", text)
            continue
        out += f"<div class='b{' me' if who == 'me' else ''}'>{h(text)}<span class='t'>10:0{'2 ✓✓' if who == 'me' else '2'}</span></div>"
    return out
