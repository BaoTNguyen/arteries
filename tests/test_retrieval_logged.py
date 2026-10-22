"""log_retrieval emits retrieval.logged before its INSERT, and neither can sink the other."""

import pytest

from arteries import pulse_emit, storage, vascular_paths


class _Cur:
    def __init__(self, executed):
        self.executed = executed

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.executed.append((sql, params))


class _Conn(_Cur):
    def cursor(self):
        return _Cur(self.executed)

    def commit(self):
        self.executed.append(("COMMIT", None))


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VASCULAR_HOME", str(tmp_path))
    monkeypatch.delenv("ARTERIES_JOURNAL", raising=False)
    monkeypatch.delenv("EVENT_JOURNAL_DIR", raising=False)
    emits, executed = [], []
    # Shared log so ordering between the emit and the INSERT is observable.
    monkeypatch.setattr(pulse_emit, "emit",
                        lambda *a, **kw: (emits.append((a, kw)), executed.append(("EMIT", None))))
    monkeypatch.setattr(storage, "_conn", lambda: _Conn(executed))
    return emits, executed


def _payload(situation, score=0.75):
    return {"project_id": "p", "agent_process_id": "a", "prompt_id": "pr",
            "score": score, "relevance": 1.0, "situation": situation,
            "situation_chars": len(situation)}


def test_emits_once_and_inserts_same_values(env, tmp_path):
    emits, executed = env
    storage.log_retrieval("p", "a", "pr", "fix the build", 0.75)
    assert len(emits) == 1
    args, kw = emits[0]
    assert args == ("arteries", "retrieval.logged", vascular_paths.journal_dir(),
                    vascular_paths.path("state", "pulse", "contexts"))
    assert str(args[2]).startswith(str(tmp_path))
    assert kw == {"payload": _payload("fix the build")}
    inserts = [e for e in executed if "INSERT INTO arteries.retrievals" in e[0]]
    assert len(inserts) == 1
    assert inserts[0][1] == ("p", "a", "pr", "fix the build", 0.75)
    assert [e[0] for e in executed][0] == "EMIT"
    assert executed[-1] == ("COMMIT", None)


def test_emit_raising_still_inserts(env, monkeypatch):
    _, executed = env

    def boom(*a, **kw):
        raise RuntimeError("journal down")

    monkeypatch.setattr(pulse_emit, "emit", boom)
    storage.log_retrieval("p", "a", "pr", "s", 0.5)
    assert any("INSERT INTO arteries.retrievals" in e[0] for e in executed)
    assert executed[-1] == ("COMMIT", None)


def test_conn_raising_still_emitted(env, monkeypatch):
    emits, _ = env

    def down():
        raise RuntimeError("no database")

    monkeypatch.setattr(storage, "_conn", down)
    # The INSERT failure still propagates, as it did before the emit existed.
    with pytest.raises(RuntimeError, match="no database"):
        storage.log_retrieval("p", "a", "pr", "s", 0.5)
    assert len(emits) == 1
    assert emits[0][1]["payload"] == _payload("s", 0.5)


def test_long_situation_carried_whole(env):
    emits, executed = env
    situation = "x" * 4984
    storage.log_retrieval("p", "a", "pr", situation, 1)
    payload = emits[0][1]["payload"]
    assert payload["situation"] == situation
    assert payload["situation_chars"] == 4984
    assert isinstance(payload["score"], float)
    insert = next(e for e in executed if "INSERT INTO arteries.retrievals" in e[0])
    assert insert[1][3] == situation


def test_journal_off_skips_emit_keeps_insert(env, monkeypatch):
    emits, executed = env
    monkeypatch.setenv("ARTERIES_JOURNAL", "off")
    storage.log_retrieval("p", "a", "pr", "s", 0.5)
    assert emits == []
    assert any("INSERT INTO arteries.retrievals" in e[0] for e in executed)
