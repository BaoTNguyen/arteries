"""journal_append's trace_id: a parent's PULSE_TRACE_ID wins, else one derived
from ARTERIES_SESSION_ID, else none."""
import hashlib
import json

import pytest

from arteries.journal import journal_append


@pytest.fixture
def lines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EVENT_JOURNAL_DIR", str(tmp_path))
    monkeypatch.delenv("ARTERIES_JOURNAL", raising=False)
    monkeypatch.delenv("PULSE_TRACE_ID", raising=False)
    monkeypatch.delenv("ARTERIES_SESSION_ID", raising=False)

    def read():
        events = [json.loads(ln) for p in sorted(tmp_path.glob("*.ndjson"))
                  for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return [e for e in events if e["kind"] != "context.started"]
    return read


def test_session_only(lines, monkeypatch):
    monkeypatch.setenv("ARTERIES_SESSION_ID", "s1")
    journal_append("arteries", "test.one")
    journal_append("arteries", "test.two")
    expected = hashlib.sha256(b"claude:s1").hexdigest()[:32]
    got = lines()
    assert len(got) == 2
    assert [e["trace_id"] for e in got] == [expected, expected]


def test_parent_wins(lines, monkeypatch):
    monkeypatch.setenv("PULSE_TRACE_ID", "abc")
    monkeypatch.setenv("ARTERIES_SESSION_ID", "s1")
    journal_append("arteries", "test.one")
    (got,) = lines()
    assert got["trace_id"] == "abc"


def test_neither_set(lines):
    journal_append("arteries", "test.one")
    (got,) = lines()
    assert "trace_id" not in got
