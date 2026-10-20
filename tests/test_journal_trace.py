"""journal_append's trace_id: a parent's PULSE_TRACE_ID wins, else one derived
from ARTERIES_SESSION_ID, else none."""
import hashlib
import io
import json

import pytest

from arteries import observe_tool, runlog
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


@pytest.mark.parametrize("tool", ["WebFetch", "web_search"])
def test_web_tool_result_is_external(lines, tool):
    journal_append("arteries", "tool.result", tool=tool)
    (got,) = lines()
    assert got["origin"] == "external"


def test_other_tool_result_has_no_origin(lines):
    journal_append("arteries", "tool.result", tool="Bash")
    (got,) = lines()
    assert "origin" not in got


@pytest.mark.writes_events
def test_observe_tool_web_search_end_to_end(lines, tmp_path, monkeypatch):
    monkeypatch.setenv("VASCULAR_HOME", str(tmp_path / "vascular"))
    monkeypatch.setenv("ARTERIES_REPO", str(tmp_path))
    monkeypatch.setenv("ARTERIES_RUNLOG", "db")
    # no Postgres here: the db write fails, is swallowed, and runlog falls back
    monkeypatch.setitem(runlog.DB_CONFIG, "host", str(tmp_path / "no-pg"))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"tool":"WebSearch","target":"search"}'))
    assert observe_tool.main() == 0
    (got,) = [e for e in lines() if e["kind"] == "tool.result"]
    assert got["origin"] == "external"
