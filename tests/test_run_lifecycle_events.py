"""The run lifecycle rides on events (pulse P5, A4): run.started/run.resumed
carry the whole run, and every close emits run.ended keyed by run_id."""
from unittest.mock import patch

from arteries import runlog


def _run():
    return runlog._run_payload("run-1", "p", "agent", "claude", runlog.Path("/tmp/r"),
                               metadata={"kind": "interactive", "session_id": "s"})


def test_start_run_carries_the_whole_run(monkeypatch):
    logged = []
    monkeypatch.setattr(runlog, "sweep_runs", lambda: [])
    monkeypatch.setattr(runlog, "current_run", lambda *a, **k: _run())
    monkeypatch.setattr(runlog, "_run_exists", lambda rid: False)
    monkeypatch.setattr(runlog, "_persist_run", lambda run: None)
    monkeypatch.setattr(runlog, "log_event", lambda kind, src, payload, **kw: logged.append((kind, payload)))
    runlog.start_run()
    kind, payload = logged[0]
    assert kind == "run.started"
    assert payload["metadata"] == {"kind": "interactive", "session_id": "s"}
    assert payload["started_at"]


def test_end_run_emits_run_ended_once(monkeypatch):
    results = iter([True, False])
    monkeypatch.setattr(runlog, "_db", lambda work, default, **k: next(results))
    with patch.object(runlog, "journal_append") as journal:
        assert runlog.end_run("run-1", "episode_finished")
        assert not runlog.end_run("run-1", "episode_finished")
    journal.assert_called_once_with("arteries", "run.ended", run_id="run-1", reason="episode_finished")


def test_the_sweep_emits_run_ended_per_closed_run(monkeypatch):
    monkeypatch.setattr(runlog, "_db", lambda work, default, **k: ["a", "b"])
    with patch.object(runlog, "journal_append") as journal:
        assert runlog.sweep_runs() == ["a", "b"]
    assert [c.kwargs["run_id"] for c in journal.call_args_list] == ["a", "b"]
    assert {c.kwargs["reason"] for c in journal.call_args_list} == {"idle_sweep"}


def test_discard_emits_nothing(monkeypatch):
    monkeypatch.setenv("ARTERIES_RUNLOG", "discard")
    monkeypatch.setattr(runlog, "_db", lambda work, default, **k: True)
    with patch.object(runlog, "journal_append") as journal:
        runlog.end_run("run-1")
    journal.assert_not_called()
