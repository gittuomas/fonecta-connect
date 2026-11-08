"""Small, bounded public-data clients. No training labels or simulator truth are read."""
from __future__ import annotations

import gzip
import hashlib
import html
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

UTC = timezone.utc
ZONES = {"FI": "Europe/Helsinki", "SE": "Europe/Stockholm", "NO": "Europe/Oslo"}
USER_AGENT = "SinceAIConnect/0.1 (https://github.com/gittuomas/sinceAI)"
ATTRIBUTIONS = {
    "helsinki": "City of Helsinki Linked Events, CC BY 4.0; normalized event facts.",
    "stockholm": "Stockholm Business Region AB, CC BY 4.0; normalized event facts.",
    "oslo": "Tangokalender Oslo and linked organizers; event facts only, source links retained.",
    "geonorge": "Kartverket open address API; approximate venue address coordinates.",
    "weather": "Weather data by Open-Meteo.com, CC BY 4.0; hourly forecast summaries.",
    "ticketmaster": "Ticketmaster Discovery API; source links retained; provider terms apply.",
}


def utcnow():
    return datetime.now(UTC)


def text(value):
    if isinstance(value, dict):
        value = next((value[k] for k in ("en", "fi", "sv", "nb", "no") if value.get(k)), "")
    return html.unescape(re.sub(r"<[^>]*>", "", str(value or ""))).strip()


def parse_time(value, country):
    if not value or "T" not in str(value):
        return None  # Date-only and unknown times cannot justify specific opening hours.
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=ZoneInfo(ZONES[country])) if dt.tzinfo is None else dt.astimezone(ZoneInfo(ZONES[country]))
    except (ValueError, TypeError):
        return None


def distance_km(a, b, c, d):
    p1, p2 = math.radians(a), math.radians(c)
    h = math.sin((p2-p1)/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(math.radians(d-b)/2)**2
    return 6371.0088 * 2 * math.asin(min(1, math.sqrt(h)))


def safe_url(url):
    return str(url) if urllib.parse.urlsplit(str(url)).scheme in ("https", "http") else ""


class JsonCache:
    def __init__(self, directory, offline=False):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.offline = offline

    def get(self, url, ttl=3600):
        # Hash includes credentials; request URLs are never used as filenames or logged.
        path = self.directory / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        age = (utcnow() - datetime.fromisoformat(saved["fetched_at"])).total_seconds() if saved else float("inf")
        if saved and age <= ttl:
            return saved["data"], saved["fetched_at"], "cached"
        if self.offline:
            if saved:
                return saved["data"], saved["fetched_at"], "stale"
            raise RuntimeError("No cached response; run refresh while online first")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=18) as response:
                raw = response.read(12_000_001)
                if len(raw) > 12_000_000:
                    raise ValueError("Provider response exceeds 12 MB limit")
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                data = json.loads(raw)
            stamp = utcnow().isoformat()
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"fetched_at": stamp, "data": data}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            return data, stamp, "live"
        except (OSError, ValueError, urllib.error.HTTPError):
            if saved:
                return saved["data"], saved["fetched_at"], "stale"
            raise RuntimeError("Provider unavailable; no cached response") from None


def event(provider, raw_id, country, title, venue, lat, lon, start, end, url, category="", outdoor=False,
          status="scheduled", fetched_at="", updated_at=None, coordinate_source=None):
    begin, finish = parse_time(start, country), parse_time(end, country)
    if not begin or lat is None or lon is None:
        return None
    try:
        lat, lon = float(lat), float(lon)
        if not math.isfinite(lat+lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
    except (ValueError, TypeError):
        return None
    if finish and (finish <= begin or finish-begin > timedelta(hours=18)):
        finish = None
    return {"id": f"{provider}:{raw_id}:{begin.isoformat()}", "provider": provider, "country": country,
            "title": text(title)[:220], "venue": text(venue)[:160], "lat": lat, "lon": lon,
            "start": begin.isoformat(), "end": finish.isoformat() if finish else None,
            "url": safe_url(url), "category": category, "outdoor": outdoor, "status": status,
            "fetched_at": fetched_at, "updated_at": updated_at, "coordinate_source": coordinate_source}


def normalize_helsinki(rows, fetched_at):
    for r in rows:
        if r.get("super_event_type") or r.get("deleted") or r.get("has_start_time") is False:
            continue
        loc = r.get("location") or {}
        coords = (loc.get("position") or {}).get("coordinates") or []
        if len(coords) != 2:
            continue
        keywords = " ".join(text(k.get("name")) for k in r.get("keywords", []))
        status = {"EventScheduled": "scheduled", "EventRescheduled": "rescheduled", "EventCancelled": "cancelled"}.get(r.get("event_status"), "unknown")
        e = event("helsinki", r["id"], "FI", r.get("name"), loc.get("name"), coords[1], coords[0],
                  r.get("start_time"), r.get("end_time") if r.get("has_end_time") is not False else None, text(r.get("info_url")) or r.get("@id", ""),
                  keywords, "outdoor" in keywords.lower(), status, fetched_at, r.get("last_modified_time"))
        if e:
            yield e


def normalize_stockholm(rows, fetched_at):
    for r in rows:
        loc = r.get("location") or {}
        category = " ".join(str(c.get("slug", "")) for c in r.get("categories", []))
        schedule = r.get("schedule") or {}
        occurrences = schedule.get("dates", []) if isinstance(schedule, dict) else []
        if not occurrences and r.get("start_date") == r.get("end_date"):
            occurrences = [{"date": r.get("start_date"), "start_time": r.get("start_time"), "end_time": r.get("end_time")}]
        for occurrence in occurrences:
            day, start, end = occurrence.get("date"), occurrence.get("start_time"), occurrence.get("end_time")
            if not day or not start:
                continue
            begin = parse_time(f"{day}T{start}", "SE")
            finish = parse_time(f"{day}T{end}", "SE") if end else None
            if begin and finish and finish < begin:
                finish += timedelta(days=1)
            e = event("stockholm", r["id"], "SE", r.get("title"), r.get("venue_name"), loc.get("latitude"), loc.get("longitude"),
                      begin.isoformat() if begin else None, finish.isoformat() if finish else None,
                      "https://www.visitstockholm.com/events/" + str(r.get("url", "")).strip("/") + "/",
                      category, "outdoor" in category, fetched_at=fetched_at, updated_at=r.get("modified_at"))
            if e:
                yield e


def geocode_oslo(cache, address):
    street = re.sub(r",.*", "", address).strip()
    if not re.search(r"\d", street):
        return None
    url = "https://ws.geonorge.no/adresser/v1/sok?" + urllib.parse.urlencode({"sok": street + " Oslo", "treffPerSide": 10})
    data, stamp, state = cache.get(url, ttl=30*86400)
    if state == "stale":
        return None
    rows = data.get("adresser", [])
    total = data.get("metadata", {}).get("totaltAntallTreff", len(rows))
    if not rows or total > len(rows) or any(r.get("kommunenavn") != "OSLO" for r in rows):
        return None
    # Multiple entrances at the same street number are OK only within 100 metres.
    points = [(r["representasjonspunkt"]["lat"], r["representasjonspunkt"]["lon"]) for r in rows]
    if any(distance_km(*points[0], *p) > .1 for p in points):
        return None
    return (sum(p[0] for p in points)/len(points), sum(p[1] for p in points)/len(points), url)


def normalize_oslo(rows, fetched_at, geocoder):
    for r in rows:
        loc = r.get("venue") or {}
        if str(loc.get("city", "")).lower() != "oslo":
            continue
        point = geocoder(loc.get("address", ""))
        if not point:
            continue
        e = event("oslo", r["id"], "NO", r.get("title"), loc.get("name"), *point[:2], r.get("start"), r.get("end"),
                  r.get("link") or r.get("source_url"), "dance", "outdoor" in r.get("types", []),
                  r.get("status", "unknown"), fetched_at, r.get("last_verified"), point[2])
        if e:
            yield e


def normalize_ticketmaster(rows, fetched_at):
    for r in rows:
        venues = r.get("_embedded", {}).get("venues", [])
        if not venues:
            continue
        v = venues[0]
        country = v.get("country", {}).get("countryCode")
        if country not in ZONES:
            continue
        loc, dates = v.get("location", {}), r.get("dates", {})
        start = dates.get("start", {})
        if start.get("dateTBD") or start.get("dateTBA") or start.get("timeTBA"):
            continue
        category = " ".join(c.get("segment", {}).get("name", "") for c in r.get("classifications", []))
        e = event("ticketmaster", r["id"], country, r.get("name"), v.get("name"), loc.get("latitude"), loc.get("longitude"),
                  start.get("dateTime"), dates.get("end", {}).get("dateTime"), r.get("url"), category,
                  status={"onsale": "scheduled", "offsale": "scheduled", "rescheduled": "rescheduled", "cancelled": "cancelled"}.get(dates.get("status", {}).get("code"), "unknown"),
                  fetched_at=fetched_at)
        if e:
            yield e


def fetch_events(cache, start, days=7, max_pages=8):
    end = start + timedelta(days=days)
    all_events, reports = [], []
    for provider in ("helsinki", "stockholm", "oslo", "ticketmaster"):
        reports.append({"provider": provider, "status": "unavailable", "events": 0, "requests": 0,
                        "attribution": ATTRIBUTIONS[provider], "warnings": []})
        report = reports[-1]
        if provider == "ticketmaster" and not os.environ.get("TICKETMASTER_API_KEY"):
            report.update(status="not_configured", warnings=["Broader Nordic ticketed events require TICKETMASTER_API_KEY."])
            continue
        collected, states, stamps = [], [], []
        try:
            if provider == "oslo":
                data, stamp, state = cache.get("https://tango.oslo.no/events.json")
                states.append(state); stamps.append(stamp); report["requests"] += 1
                # Geocode only the requested date window, memoized per venue address.
                memo = {}
                def geo(address):
                    if address not in memo:
                        memo[address] = geocode_oslo(cache, address)
                    return memo[address]
                data = [r for r in data if start.isoformat() <= str(r.get("start", ""))[:10] < end.isoformat()]
                collected.extend(normalize_oslo(data, stamp, geo))
                report["warnings"].append("Oslo tango/community events only; not a comprehensive Norway feed. Venue positions are address-level estimates.")
            else:
                countries = list(ZONES) if provider == "ticketmaster" else [None]
                for country in countries:
                    for page in range(1, max_pages+1):
                        if provider == "helsinki":
                            url = "https://api.hel.fi/linkedevents/v1/event/?" + urllib.parse.urlencode({"start": start.isoformat(), "end": end.isoformat(), "include": "location,keywords", "page_size": 100, "page": page, "sort": "-start_time", "format": "json"})
                        elif provider == "stockholm":
                            url = f"https://api.visitstockholm.com/api/public-v1/events/?size=100&page={page}"
                        else:
                            time.sleep(.21)  # Stay below Ticketmaster's five requests/second limit.
                            begin_utc = datetime.combine(start, datetime.min.time(), ZoneInfo(ZONES[country])).astimezone(UTC)
                            end_utc = datetime.combine(end, datetime.min.time(), ZoneInfo(ZONES[country])).astimezone(UTC)
                            url = "https://app.ticketmaster.com/discovery/v2/events.json?" + urllib.parse.urlencode({"apikey": os.environ["TICKETMASTER_API_KEY"], "countryCode": country, "startDateTime": begin_utc.strftime('%Y-%m-%dT%H:%M:%SZ'), "endDateTime": end_utc.strftime('%Y-%m-%dT%H:%M:%SZ'), "size": 100, "page": page-1, "sort": "date,asc"})
                        data, stamp, state = cache.get(url)
                        states.append(state); stamps.append(stamp); report["requests"] += 1
                        if provider == "helsinki":
                            collected.extend(normalize_helsinki(data.get("data", []), stamp)); more = bool(data.get("meta", {}).get("next"))
                        elif provider == "stockholm":
                            collected.extend(normalize_stockholm(data.get("results", []), stamp)); more = bool(data.get("next"))
                        else:
                            collected.extend(normalize_ticketmaster(data.get("_embedded", {}).get("events", []), stamp)); more = page < data.get("page", {}).get("totalPages", 0)
                        if not more:
                            break
                        if page == max_pages:
                            report["warnings"].append("Page limit reached; event coverage is partial.")
            collected = [e for e in collected if start.isoformat() <= e["start"][:10] < end.isoformat()]
            # Preserve cancelled records for provenance; context consumers exclude them.
            report.update(status="stale" if "stale" in states else ("live" if "live" in states else "cached"),
                          fetched_at=min(stamps) if stamps else None, events=len(collected))
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            report["warnings"].append("Provider response unavailable or incompatible; retained events are excluded from current model context.")
            report["status"] = "unavailable"
        for e in collected:
            e["source_status"] = report["status"]
        all_events.extend(collected)
    return deduplicate(all_events), reports


def deduplicate(events):
    unique = {}
    for e in events:
        # Duplicate publications share title, actual start and venue position; occurrences stay distinct.
        key = (e["country"], re.sub(r"\W", "", e["title"]).casefold(), e["start"], round(e["lat"], 3), round(e["lon"], 3))
        # A cancellation wins over a second publisher's still-active copy.
        if key not in unique or e["status"] == "cancelled":
            unique[key] = e
    return list(unique.values())
