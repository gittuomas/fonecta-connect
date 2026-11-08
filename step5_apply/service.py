#!/usr/bin/env python3
"""Step 5 over HTTP (JSON), so steps 1–4 can be written in any language.

    python -m step5_apply.service                      # http://127.0.0.1:8790, starts its own Yext mock
    python -m step5_apply.service --mock-url http://127.0.0.1:8787   # use the team's running mock instead
    python -m step5_apply.service --no-sandbox         # don't mirror writes to the Yext sandbox

    POST /apply      Decision JSON                         -> result (items, message, audit_id, mirror)
    POST /undo       {"audit_id"} or {"entity_id"}         -> result
    POST /reply      {"entity_id", "text"}                 -> result if the text is an undo word, else {"undo": false}
    GET  /snapshot   ?entity_id=FI-D0001&dates=2026-12-24,2026-12-31
    GET  /policy     ?entity_id=FI-D0012&date=2026-12-24&hours=closed
    GET  /where      ?entity_id=FI-D0001                   -> publishers + typical update speed
    GET  /audit      ?entity_id=FI-D0001
    GET  /health

Binds to 127.0.0.1 only and has no auth: it's a local demo component. Contract: step5_apply/INTEGRATION.md.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .api import Step5

STEP5: Step5 | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "ConnectStep5/1.0"
    quiet = False

    def log_message(self, fmt, *args):
        if not self.quiet:
            sys.stderr.write("[step5] " + fmt % args + "\n")

    def _send(self, code: int, obj) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n).decode() or "{}") if n else {}
        except ValueError:
            raise ValueError("body must be JSON") from None

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        self._safe(self._get)

    def do_POST(self):
        self._safe(self._post)

    def _safe(self, fn):
        try:
            fn()
        except ValueError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:  # never drop the connection; the caller decides what to tell the owner
            traceback.print_exc()
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _get(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path.rstrip("/")
        s5 = STEP5
        if p in ("", "/health"):
            a = s5.applier
            return self._send(200, {"ok": True, "primary": a.primary.base,
                                    "sandbox": a.sandbox.base if a.sandbox else None, "mirror": a.mirror_map})
        eid = q.get("entity_id")
        if p == "/audit":
            return self._send(200, {"records": s5.audit(eid)})
        if not eid:
            raise ValueError("'entity_id' query parameter is required")
        if p == "/snapshot":
            dates = [d for d in q.get("dates", "").split(",") if d]
            if not dates:
                raise ValueError("'dates' query parameter is required (comma-separated YYYY-MM-DD)")
            return self._send(200, {"entity_id": eid, "seen_before": s5.snapshot(eid, dates)})
        if p == "/policy":
            return self._send(200, s5.policy(eid, q.get("day_key", ""), q.get("hours"), q.get("date", "")))
        if p == "/where":
            return self._send(200, {"entity_id": eid, "publishers": s5.where_it_shows(eid)})
        self._send(404, {"error": f"unknown endpoint GET {p}"})

    def _post(self):
        p = urlparse(self.path).path.rstrip("/")
        body = self._body()
        s5 = STEP5
        if p == "/apply":
            return self._send(200, s5.apply(body))
        if p == "/undo":
            return self._send(200, s5.undo(body.get("audit_id"), body.get("entity_id"), body.get("language")))
        if p == "/reply":
            if not body.get("entity_id"):
                raise ValueError("'entity_id' is required")
            res = s5.handle_reply(body["entity_id"], body.get("text", ""), body.get("language"))
            return self._send(200, res if res is not None else {"undo": False})
        self._send(404, {"error": f"unknown endpoint POST {p}"})


def serve(step5: Step5, port: int = 8790, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    global STEP5
    STEP5 = step5
    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--mock-url", default=None, help="use a running Yext mock instead of starting one")
    ap.add_argument("--no-sandbox", action="store_true", help="don't mirror writes to the Yext sandbox")
    ap.add_argument("--runtime-dir", default=None, help="where the audit log and permissions live (default step5_apply/.runtime/)")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    s5 = Step5.from_env(mock_url=a.mock_url, use_sandbox=False if a.no_sandbox else None, runtime_dir=a.runtime_dir)
    httpd = serve(s5, a.port)
    ap_ = s5.applier
    print(f"Step 5 service on http://127.0.0.1:{a.port}")
    print(f"  Yext (primary): {ap_.primary.base}" + ("  (in-process mock)" if s5.mock else ""))
    print(f"  Yext sandbox  : {'on, mirror ' + json.dumps(ap_.mirror_map, ensure_ascii=False) if ap_.sandbox else 'off'}",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        s5.close()


if __name__ == "__main__":
    main()
