"""journal_append writes pulse's envelope, through the vendored pulse_emit."""
import json
from pathlib import Path

import pytest

from arteries import pulse_emit
from arteries.journal import journal_append

ENVELOPE = json.loads((Path(__file__).parent / "pulse_envelope.json").read_text())
FIELDS = {**ENVELOPE["required"], **ENVELOPE["optional"]}


def _type_ok(kind: str, value) -> bool:
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    if kind == "object":
        return isinstance(value, dict)
    return isinstance(value, str)  # string, uuid, timestamp, name, kind, origin


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setenv("VASCULAR_HOME", str(tmp_path / "vhome"))
    monkeypatch.setenv("EVENT_JOURNAL_DIR", str(tmp_path / "journal"))
    for key in ("ARTERIES_JOURNAL", "ARTERIES_TRUST", "ARTERIES_EPISODE_ID", "ARTERIES_TASK_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(pulse_emit, "_announced", set())

    def read() -> list[dict]:
        return [json.loads(line)
                for p in sorted((tmp_path / "journal").glob("*.ndjson"))
                for line in p.read_text().splitlines()]
    return read


def test_every_line_matches_the_envelope(journal, monkeypatch):
    monkeypatch.setenv("ARTERIES_EPISODE_ID", "ep-1")
    journal_append("arteries", "turn.observed", turn_id="t1", event_id="e1",
                   run_id="r1", note="hi")
    journal_append("arteries", "decision.retrieval.gate")
    lines = journal()
    assert len(lines) == 3
    for line in lines:
        assert isinstance(line, dict)
        for key, value in line.items():
            assert key in FIELDS, key
            assert _type_ok(FIELDS[key], value), (key, value)
        for key in ("id", "schema_version", "emitter_seq", "context_id"):
            assert key in line
        assert line["schema_version"] == 1
    assert [e["kind"] for e in lines].count("context.started") == 1


def test_fields_set_as_before(journal, monkeypatch):
    monkeypatch.setenv("ARTERIES_EPISODE_ID", "ep-1")
    monkeypatch.setenv("ARTERIES_TASK_ID", "task-1")
    journal_append("arteries", "turn.observed", turn_id="t1", event_id="e1",
                   run_id="r1", note="hi", chars=3)
    ev = next(e for e in journal() if e["kind"] == "turn.observed")
    assert ev["source"] == "arteries"
    assert ev["ts"].endswith("+00:00")
    assert ev["id"] == "e1"
    assert ev["run_id"] == "r1"
    assert ev["episode_id"] == "ep-1"
    assert ev["task_id"] == "task-1"
    assert ev["turn_id"] == "t1"
    assert ev["payload"] == {"note": "hi", "chars": 3}


def test_empty_values_stay_absent(journal):
    journal_append("arteries", "decision.retrieval.gate", turn_id="", run_id="")
    ev = next(e for e in journal() if e["kind"] == "decision.retrieval.gate")
    for key in ("run_id", "episode_id", "task_id", "turn_id", "payload", "origin"):
        assert key not in ev
    assert ev["id"]  # pulse_emit's fresh uuid


def test_journal_off_writes_nothing(journal, monkeypatch, tmp_path):
    monkeypatch.setenv("ARTERIES_JOURNAL", "off")
    journal_append("arteries", "turn.observed", note="hi")
    assert not (tmp_path / "journal").exists()


@pytest.mark.parametrize("kind, trust, origin", [
    ("turn.observed", None, "operator"),
    ("turn.observed", "untrusted", "agent"),
    ("assistant.response", None, "agent"),
    ("decision.retrieval.gate", None, None),
])
def test_origin_by_kind(journal, monkeypatch, kind, trust, origin):
    if trust:
        monkeypatch.setenv("ARTERIES_TRUST", trust)
    journal_append("arteries", kind)
    ev = next(e for e in journal() if e["kind"] == kind)
    assert ev.get("origin") == origin
