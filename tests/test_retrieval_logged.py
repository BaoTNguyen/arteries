"""log_retrieval writes retrieval.logged and nothing else: arteries.retrievals
is a read-only archive since pulse P5 (migration 020)."""

from pathlib import Path

import pytest

from arteries import pulse_emit, storage, vascular_paths


@pytest.fixture
def emits(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VASCULAR_HOME", str(tmp_path))
    monkeypatch.delenv("ARTERIES_JOURNAL", raising=False)
    monkeypatch.delenv("EVENT_JOURNAL_DIR", raising=False)
    out = []
    monkeypatch.setattr(pulse_emit, "emit", lambda *a, **kw: out.append((a, kw)))

    def no_db():
        raise AssertionError("log_retrieval must not touch the database")

    monkeypatch.setattr(storage, "_conn", no_db)
    return out


def _payload(situation, score=0.75):
    return {"project_id": "p", "agent_process_id": "a", "prompt_id": "pr",
            "score": score, "relevance": 1.0, "situation": situation,
            "situation_chars": len(situation)}


def test_emits_once_and_writes_no_row(emits, tmp_path):
    storage.log_retrieval("p", "a", "pr", "fix the build", 0.75)
    assert len(emits) == 1
    args, kw = emits[0]
    assert args == ("arteries", "retrieval.logged", vascular_paths.journal_dir(),
                    vascular_paths.path("state", "pulse", "contexts"))
    assert str(args[2]).startswith(str(tmp_path))
    assert kw == {"payload": _payload("fix the build")}


def test_emit_raising_never_reaches_the_caller(emits, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("journal down")

    monkeypatch.setattr(pulse_emit, "emit", boom)
    storage.log_retrieval("p", "a", "pr", "s", 0.5)


def test_long_situation_carried_whole(emits):
    situation = "x" * 4984
    storage.log_retrieval("p", "a", "pr", situation, 1)
    payload = emits[0][1]["payload"]
    assert payload["situation"] == situation
    assert payload["situation_chars"] == 4984
    assert isinstance(payload["score"], float)


def test_journal_off_records_nothing(emits, monkeypatch):
    monkeypatch.setenv("ARTERIES_JOURNAL", "off")
    storage.log_retrieval("p", "a", "pr", "s", 0.5)
    assert emits == []


def test_the_archive_migration_blocks_every_write():
    sql = (Path(__file__).parents[1] / "migrations" / "020_retrievals_read_only.sql").read_text()
    assert sql.startswith("-- destructive")
    assert "BEFORE INSERT OR UPDATE OR DELETE ON arteries.retrievals" in sql
