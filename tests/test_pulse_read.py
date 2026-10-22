import http.server
import json
import shutil
import socketserver
import tempfile
import threading
import urllib.parse
from pathlib import Path

import pytest

from arteries import pulse_read, vascular_paths


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def _serve(monkeypatch, tmp_path, handle):
    """Start a throwaway AF_UNIX HTTP server where pulse_read will look.
    handle(handler) answers each request. Returns a stop() callable."""
    monkeypatch.setenv("VASCULAR_HOME", str(tmp_path))
    sock = tmp_path / "state" / "pulse" / "pulse.sock"
    scratch = None
    if len(str(sock)) > 100:  # AF_UNIX paths cap near 108 bytes
        scratch = tempfile.mkdtemp(dir="/tmp")
        sock = Path(scratch) / "pulse.sock"
        monkeypatch.setattr(vascular_paths, "path", lambda *a: sock)
    sock.parent.mkdir(parents=True, exist_ok=True)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            handle(self)

        def log_message(self, *a):
            pass

    server = _Server(str(sock), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def stop():
        server.shutdown()
        server.server_close()
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)

    return stop


def _reply(h, status, body: bytes):
    h.send_response(status)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


def test_happy_path(monkeypatch, tmp_path):
    seen = {}
    body = {
        "events": [
            {"seq": 7, "kind": "k", "ts": "2026-10-22T10:00:00Z"},
            {"seq": 8, "kind": "k", "ts": "2026-10-22T10:00:01+00:00"},
        ],
        "next": 8,
    }

    def handle(h):
        seen["path"] = h.path
        _reply(h, 200, json.dumps(body).encode())

    stop = _serve(monkeypatch, tmp_path, handle)
    try:
        got = pulse_read.events(kind="k", source="s", order="desc", before=99, limit=10)
    finally:
        stop()
    url = urllib.parse.urlsplit(seen["path"])
    assert url.path == "/events"
    assert urllib.parse.parse_qs(url.query) == {
        "kind": ["k"], "source": ["s"], "order": ["desc"], "before": ["99"], "limit": ["10"],
    }
    assert [e["seq"] for e in got] == [7, 8]
    assert got[0]["ts"] == "2026-10-22T10:00:00+00:00"
    assert got[1]["ts"] == "2026-10-22T10:00:01+00:00"


def test_no_socket(monkeypatch, tmp_path):
    monkeypatch.setenv("VASCULAR_HOME", str(tmp_path))
    assert pulse_read.events() is None


def test_500_status(monkeypatch, tmp_path):
    stop = _serve(monkeypatch, tmp_path, lambda h: _reply(h, 500, b'{"events": []}'))
    try:
        assert pulse_read.events() is None
    finally:
        stop()


def test_bad_json(monkeypatch, tmp_path):
    stop = _serve(monkeypatch, tmp_path, lambda h: _reply(h, 200, b"not json"))
    try:
        assert pulse_read.events() is None
    finally:
        stop()


def test_never_replies(monkeypatch, tmp_path):
    release = threading.Event()
    stop = _serve(monkeypatch, tmp_path, lambda h: release.wait(5))
    try:
        assert pulse_read.events(timeout=0.2) is None
    finally:
        release.set()
        stop()


def test_close_failure_does_not_raise(monkeypatch, tmp_path):
    stop = _serve(monkeypatch, tmp_path, lambda h: _reply(h, 200, b'{"events": []}'))

    def boom(self):
        raise OSError("close failed")

    monkeypatch.setattr(pulse_read._UnixHTTPConnection, "close", boom)
    try:
        # http.client also closes internally, so None is fine; raising isn't
        assert pulse_read.events() in (None, [])
    finally:
        stop()


def test_defaults_and_project_in_query(monkeypatch, tmp_path):
    seen = []

    def handle(h):
        seen.append(urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query))
        _reply(h, 200, b'{"events": []}')

    stop = _serve(monkeypatch, tmp_path, handle)
    try:
        assert pulse_read.events() == []
        assert pulse_read.events(project="p", order=None, limit=None) == []
    finally:
        stop()
    assert seen == [{"order": ["asc"], "limit": ["500"]}, {"project": ["p"]}]


@pytest.mark.parametrize("body", [b'{"next": 3}', b'{"events": {"seq": 1}}', b"[]", b"null"])
def test_body_without_events_list(monkeypatch, tmp_path, body):
    stop = _serve(monkeypatch, tmp_path, lambda h: _reply(h, 200, body))
    try:
        assert pulse_read.events() is None
    finally:
        stop()


def test_connect_refused(monkeypatch, tmp_path):
    import socket

    monkeypatch.setenv("VASCULAR_HOME", str(tmp_path))
    sock = vascular_paths.path("state", "pulse", "pulse.sock")
    if len(str(sock)) > 100:
        pytest.skip("socket path too long for AF_UNIX")
    sock.parent.mkdir(parents=True, exist_ok=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(sock))  # stale socket file: bound, never listening
    s.close()
    assert pulse_read.events() is None
