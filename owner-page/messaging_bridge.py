"""Connect the current owner app to the durable real WhatsApp transport."""
import argparse
import json
import threading
from datetime import date
from urllib.parse import quote

import messaging


def prepare(app, eid, pid, source):
    if eid not in app.BL.ents or source.policy(eid)["opted_out"]:
        raise messaging.MessagingError("No eligible business")
    # Real outreach uses model proposals, never the social-post example fixtures.
    items = []
    for day in app.TARGETS.get(eid, []):
        s = app.suggestion(eid, day)
        if (day >= date.today() and s["action"] == "SUGGEST"
                and s["contact_eligible"] and app.period_of(s)[0] == pid):
            items.append(s)
    seen = source.snapshot(eid, [s["day"].isoformat() for s in items]) if items else {}
    items = [s for s in items if seen.get(s["day"].isoformat()) != s["hours"]]
    if not items:
        raise messaging.MessagingError("No outstanding future suggestion for this period")
    title = app.period_of(items[0])[1]
    parts = [app.intro(pid, title, items), "Please review these proposed special opening hours."]
    for s in items:
        hours = "closed" if s["hours"] == "closed" else app.nice(s["hours"])
        history = " (same as your recorded hours last year)" if s["last_hours"] == s["hours"] else ""
        parts.append(f"{s['weekday'][:3]} {s['dm']} ({s['day_name']}): {hours}{history}.")
    return {"name": items[0]["name"], "detail": " ".join(parts),
            "question": f"Shall we update your {title} hours?",
            "items": [{"date": s["day"].isoformat(), "hours": s["hours"],
                       "day_name": s["day_name"]} for s in items]}


def parse(app, text, frozen_items):
    items = [{"day": date.fromisoformat(s["date"]), "day_name": s.get("day_name", "")}
             for s in frozen_items]
    if app.hours_ai.configured():
        result = app.hours_ai.read_reply(text, items)
        if result is None:
            return {}, ["AI reply reader unavailable; no change saved"]
        found, unclear = result
        problems = [unclear] if unclear else []
    else:
        found, problems = app.parse_hours_reply(text, items)
    return {d.isoformat(): hours for d, hours in found.items()}, problems


def webhook(handler, service):
    code = 200
    try:
        if service is None:
            code = 503
        else:
            size = int(handler.headers.get("Content-Length", "0"))
            if not 0 < size <= 65536 or handler.headers.get_content_type() != "application/x-www-form-urlencoded":
                raise ValueError("Invalid webhook body")
            service.webhook(handler.path, handler.rfile.read(size), handler.headers.get("X-Twilio-Signature", ""))
    except PermissionError:
        code = 403
    except (ValueError, UnicodeError):
        code = 400
    except Exception:
        code = 503
    raw = b"<Response></Response>" if code == 200 else b"Webhook unavailable or invalid"
    handler.send_response(code)
    handler.send_header("Content-Type", "text/xml; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def launch(app):
    parser = argparse.ArgumentParser()
    parser.add_argument("--send-whatsapp", nargs=2, metavar=("BUSINESS", "PERIOD"))
    parser.add_argument("--messaging-status", action="store_true")
    args = parser.parse_args()
    cfg = messaging.configuration()
    service = None
    if cfg["configured"]:
        try:
            service = messaging.from_env(lambda eid, pid, source: prepare(app, eid, pid, source),
                                         lambda text, items: parse(app, text, items))
        except (ValueError, RuntimeError, ImportError, OSError):
            cfg["reason"] = "Messaging setup unavailable; browser demo remains ready"
    else:
        cfg["reason"] = "Messaging configuration unavailable"
    if args.messaging_status:
        print(json.dumps({"configuration": cfg, "state": service.status() if service else None}, indent=2))
        app.S5.close()
        raise SystemExit(0)
    if args.send_whatsapp:
        try:
            if service:
                print(json.dumps(service.send_proposal(*args.send_whatsapp)))
            else:
                eid, pid = args.send_whatsapp
                print(json.dumps({"status": "preview_ready", "sent": False,
                                  "preview_url": f"http://localhost:{app.PORT}/w/{quote(eid)}/{quote(pid)}",
                                  "reason": cfg["reason"]}))
        except Exception:
            print("Submission not confirmed. Inspect --messaging-status before any retry.")
            raise SystemExit(2)
        finally:
            app.S5.close()
        raise SystemExit(0)
    if service:
        threading.Thread(target=service.run, name="real-whatsapp", daemon=True).start()
    return service
