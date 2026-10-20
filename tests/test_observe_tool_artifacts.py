"""observe_tool stores tool input and output as artifacts and journals only
their digests."""
import hashlib
import io
import json

import pytest

from arteries import observe_tool, pulse_emit, runlog

EVENT = {"tool": "Edit", "target": "a.py", "tool_input": {"file_path": "a.py"},
         "tool_response": {"ok": True, "text": "SECRET-OUTPUT"}}


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EVENT_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setenv("VASCULAR_HOME", str(tmp_path / "vascular"))
    monkeypatch.setenv("ARTERIES_REPO", str(tmp_path))
    monkeypatch.setenv("ARTERIES_RUNLOG", "db")
    monkeypatch.delenv("ARTERIES_JOURNAL", raising=False)
    # no Postgres here: the db write fails, is swallowed, and runlog falls back
    monkeypatch.setitem(runlog.DB_CONFIG, "host", str(tmp_path / "no-pg"))

    def go():
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(EVENT)))
        assert observe_tool.main() == 0
        (line,) = [ln for p in sorted(tmp_path.glob("*.ndjson"))
                   for ln in p.read_text(encoding="utf-8").splitlines()
                   if ln.strip() and json.loads(ln)["kind"] == "tool.result"]
        return line
    return go


@pytest.mark.writes_events
def test_artifacts_hold_content_and_journal_holds_digests(run, tmp_path):
    line = run()
    payload = json.loads(line)["payload"]
    for key, expected in (("input_artifact", EVENT["tool_input"]),
                          ("output_artifact", EVENT["tool_response"])):
        digest = payload[key]
        data = (tmp_path / "artifacts" / digest).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest
        assert json.loads(data) == expected
    assert "SECRET-OUTPUT" not in line


@pytest.mark.writes_events
def test_artifact_failure_still_journals(run, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(pulse_emit, "artifact", boom)
    payload = json.loads(run())["payload"]
    assert payload["tool"] == "Edit"
    assert "input_artifact" not in payload and "output_artifact" not in payload
