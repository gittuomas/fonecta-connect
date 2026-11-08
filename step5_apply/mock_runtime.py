"""Run the challenge's Yext mock in-process on a free port, with a throw-away state file (never touches
mock_api/state.json)."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import urllib.request
from dataclasses import dataclass
from http.server import ThreadingHTTPServer

from .config import MATERIALS


@dataclass
class MockServer:
    url: str
    httpd: ThreadingHTTPServer
    module: object  # mock_api/server.py, for tests that inspect STORE.update_log

    def reset(self) -> None:
        # Empty body on purpose: the mock's /mock/reset never reads the body, and on Windows a socket closed with
        # unread input sends RST, so the client intermittently got ConnectionAbortedError (WinError 10053).
        req = urllib.request.Request(f"{self.url}/mock/reset", data=b"", method="POST")
        urllib.request.urlopen(req, timeout=10).read()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def start_mock(state_dir: str | None = None) -> MockServer:
    sys.path.insert(0, str(MATERIALS / "mock_api"))
    os.environ.setdefault("MOCK_QUIET", "1")
    import server  # noqa: E402  (mock_api/server.py)

    server.STATE_FILE = os.path.join(state_dir or tempfile.mkdtemp(), "mock_state.json")
    server.STORE = server.Store()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return MockServer(f"http://127.0.0.1:{httpd.server_address[1]}", httpd, server)
