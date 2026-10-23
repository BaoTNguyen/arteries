"""Read one page of events from the pulse collector's unix socket.

Stdlib only, modeled on heart's pulse.socket_events. events() never raises:
None means pulse could not answer, so the caller can fall back.
"""
from __future__ import annotations

import http.client
import json
import socket
import urllib.parse

from arteries import vascular_paths


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, sock_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.sock_path = sock_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.sock_path)


def events(
    kind: str | None = None,
    project: str | None = None,
    source: str | None = None,
    order: str | None = "asc",
    before: str | int | None = None,
    limit: int | None = 500,
    timeout: float = 2.0,
    run: str | None = None,
    trace: str | None = None,
) -> list[dict] | None:
    """GET /events once and return its `events` list (pulse's `next` is
    ignored), or None on any failure."""
    conn = None
    try:
        # resolved per call so a VASCULAR_HOME set later (tests) applies
        sock_path = str(vascular_paths.path("state", "pulse", "pulse.sock"))
        params = (
            ("kind", kind), ("project", project), ("source", source),
            ("order", order), ("before", before), ("limit", limit),
            ("run", run), ("trace", trace),
        )
        query = urllib.parse.urlencode([(k, v) for k, v in params if v is not None])
        conn = _UnixHTTPConnection(sock_path, timeout)
        conn.request("GET", "/events?" + query)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            return None
        page = json.loads(body).get("events")
        if not isinstance(page, list):
            return None
        for e in page:
            # pulse writes UTC as Z; arteries compares ts strings in +00:00
            ts = e.get("ts")
            if isinstance(ts, str) and ts.endswith("Z"):
                e["ts"] = ts[:-1] + "+00:00"
        return page
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
