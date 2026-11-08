"""Yext Management API client (standard library only). Same code for the local mock and the team sandbox.

Follows the documented conventions:
- key in the `api-key` HEADER (never in the URL, so it can't leak into logs, screenshots or the demo video)
- fixed `v` version date
- 429: wait for Rate-Limit-Retry-At / Rate-Limit-Reset when that is soon (<= MAX_429_WAIT); otherwise fail fast,
  because the limit is a rolling hourly window and short retries would only burn more requests
- 500/502/503/504 and network errors (refused, reset, timeout, truncated response): back off and retry
- 4xx other than 429: no retry, raise YextError with Yext's own error list
- Every failure is a YextError, never a bare socket/http.client/JSON exception. `status` is None when the outcome is
  unknown (network error, unreadable 2xx body): a write may or may not have landed, so callers should read back.
"""
from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import YEXT_V

RETRY_STATUS = {500, 502, 503, 504}
MAX_429_WAIT = 30.0   # seconds; a later reset means the hourly budget is spent, so give up instead of hammering


class YextError(Exception):
    def __init__(self, status: int | None, message: str, errors: list | None = None):
        super().__init__(f"Yext {status}: {message}")
        self.status, self.errors = status, errors or []


class YextClient:
    def __init__(self, base_url: str, api_key: str, v: str = YEXT_V, account: str = "me", name: str = "",
                 timeout: float = 30, max_retries: int = 3, sleep=time.sleep):
        self.base = base_url.rstrip("/")
        self.key, self.v, self.account = api_key, v, account
        self.name = name or self.base
        self.timeout, self.max_retries, self.sleep = timeout, max_retries, sleep

    # ------------------------------------------------------------------ transport
    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        """Return the full envelope {"meta": {...}, "response": {...}}."""
        q = urllib.parse.urlencode({"v": self.v, **(params or {})})
        url = f"{self.base}/v2/accounts/{self.account}/{path}?{q}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"api-key": self.key, "Content-Type": "application/json"}
        for attempt in range(self.max_retries + 1):
            last = attempt == self.max_retries
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read().decode(errors="replace")
            except urllib.error.HTTPError as e:
                raw = e.read().decode(errors="replace")
                errors = _errors(raw)
                if e.code == 429 and not last:
                    wait = _retry_after(e.headers)
                    if wait <= MAX_429_WAIT:
                        self.sleep(wait)
                        continue
                    raise YextError(429, f"{method} {path}: rate limited, retry in {wait:.0f} s", errors) from None
                if e.code in RETRY_STATUS and not last:
                    self.sleep(2 ** attempt)
                    continue
                msg = "; ".join(x.get("message", "") for x in errors) or raw[:300]
                raise YextError(e.code, f"{method} {path}: {msg}", errors) from None
            except (OSError, http.client.HTTPException) as e:
                # URLError, timeouts, ConnectionResetError, RemoteDisconnected, IncompleteRead, ...
                if last:
                    raise YextError(None, f"{method} {path}: {type(e).__name__}: {e}") from None
                self.sleep(2 ** attempt)
                continue
            try:
                return json.loads(raw or "{}")
            except ValueError:
                raise YextError(None, f"{method} {path}: response is not JSON: {raw[:200]!r}") from None
        raise YextError(None, f"{method} {path}: retries exhausted")  # pragma: no cover

    # ------------------------------------------------------------------ endpoints
    def get_entity(self, entity_id: str) -> dict:
        return self.request("GET", f"entities/{urllib.parse.quote(entity_id, safe='')}")["response"]

    def update_entity(self, entity_id: str, fields: dict) -> dict:
        """PUT only the given top-level fields. For `hours`, always pass the FULL hours object."""
        return self.request("PUT", f"entities/{urllib.parse.quote(entity_id, safe='')}", body=fields)

    def listings(self, entity_ids: list[str]) -> list[dict]:
        """Listings: List. `entityIds` is the current filter name (`locationIds` is the legacy one; the mock takes both)."""
        return self.request("GET", "listings/listings", {"entityIds": ",".join(entity_ids)})["response"].get(
            "listings", [])


class MappedYextClient:
    """Use synthetic challenge IDs with explicitly paired sandbox entities.

    Never fall back to a challenge ID on the real API. One-to-one mappings also
    preserve the Applier's per-entity locking and audit/undo isolation.
    """
    def __init__(self, client: YextClient, mapping: dict[str, str]):
        if not mapping or any(not key or not value for key, value in mapping.items()):
            raise ValueError("Sandbox requires non-empty entity mappings")
        if len(set(mapping.values())) != len(mapping):
            raise ValueError("Sandbox entity mappings must be one-to-one")
        self.client, self.mapping = client, dict(mapping)
        self.base, self.name = client.base, "sandbox"

    def _target(self, entity_id: str) -> str:
        if entity_id not in self.mapping:
            raise YextError(None, f"No sandbox entity mapping for {entity_id}")
        return self.mapping[entity_id]

    def get_entity(self, entity_id: str) -> dict:
        return self.client.get_entity(self._target(entity_id))

    def update_entity(self, entity_id: str, fields: dict) -> dict:
        return self.client.update_entity(self._target(entity_id), fields)

    def listings(self, entity_ids: list[str]) -> list[dict]:
        return self.client.listings([self._target(eid) for eid in entity_ids])

def _errors(raw: str) -> list:
    try:
        return json.loads(raw).get("meta", {}).get("errors", [])
    except (ValueError, AttributeError):
        return []


def _retry_after(headers) -> float:
    """Seconds to wait after a 429 (at least 1). Header values may be epoch seconds or a delta."""
    for h in ("Rate-Limit-Retry-At", "Rate-Limit-Reset"):
        val = headers.get(h) if headers else None
        if val:
            try:
                x = float(val)
            except ValueError:
                continue
            wait = x - time.time() if x > 1e9 else x
            return max(1.0, wait)
    return 5.0


def fatal_or_field_errors(envelope: dict) -> list[dict]:
    """Errors in a 2xx response that mean something was NOT saved (FATAL_ERROR / NON_FATAL_ERROR, not WARNING)."""
    return [e for e in envelope.get("meta", {}).get("errors", []) if e.get("type") != "WARNING"]
