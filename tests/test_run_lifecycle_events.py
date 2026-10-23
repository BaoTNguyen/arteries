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


def _ev(kind, run_id, **payload):
    return {"kind": kind, "run_id": run_id, "payload": payload}


def _pulse(monkeypatch, pages):
    """pages: {(kind, run, trace): events}; anything else answers []."""
    calls = []

    def events(**kw):
        calls.append(kw)
        return pages.get((kw.get("kind"), kw.get("run"), kw.get("trace")), [])

    monkeypatch.setattr(runlog.pulse_read, "events", events)
    return calls


def test_a_run_ended_on_pulse_is_closed_without_the_table(monkeypatch):
    _pulse(monkeypatch, {("run.ended", "r1", None): [_ev("run.ended", "r1", reason="x")]})
    monkeypatch.setattr(runlog, "_db", lambda *a, **k: (_ for _ in ()).throw(AssertionError("table read")))
    assert runlog._run_is_open("r1") is False


def test_no_run_ended_falls_through_to_the_table(monkeypatch):
    _pulse(monkeypatch, {})
    monkeypatch.setattr(runlog, "_db", lambda work, default, **k: "table says")
    assert runlog._run_is_open("r1") == "table says"


def test_the_sessions_newest_open_run_comes_from_pulse(monkeypatch):
    trace = runlog.session_trace("sess-A")
    full = dict(started_at="2026-10-23T00:00:00+00:00", metadata={"kind": "interactive"},
                project_id="p", repo="/r", cli="claude", agent_id="a")
    _pulse(monkeypatch, {
        ("run.*", None, trace): [_ev("run.resumed", "new", **full), _ev("run.started", "new", **full),
                                 _ev("run.started", "old", **full)],
        ("run.ended", "new", None): [_ev("run.ended", "new")],
    })
    monkeypatch.setattr(runlog, "_db", lambda work, default, **k: True)  # table: still open
    got = runlog._session_run("sess-A", open_only=True)
    assert got["id"] == "old" and got["metadata"] == {"kind": "interactive"}
    assert runlog._session_run("sess-A", open_only=False)["id"] == "new"


def test_a_session_whose_events_predate_the_record_asks_the_table(monkeypatch):
    trace = runlog.session_trace("sess-A")
    _pulse(monkeypatch, {("run.*", None, trace): [_ev("run.started", "r", store="db")]})
    monkeypatch.setattr(runlog, "_db", lambda work, default, **k: {"id": "from-table"})
    assert runlog._session_run("sess-A")["id"] == "from-table"


def test_idle_runs_from_pulse(monkeypatch):
    from datetime import datetime, timezone
    now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
    meta = lambda kind: {"started_at": "x", "metadata": {"kind": kind}}  # noqa: E731
    page = [
        {"kind": "run.ended", "run_id": "ended", "ts": "2026-10-20T00:00:00+00:00", "payload": {}},
        {"kind": "run.started", "run_id": "ended", "ts": "2026-10-19T00:00:00+00:00", "payload": meta("interactive")},
        {"kind": "run.started", "run_id": "quiet-ep", "ts": "2026-10-23T09:00:00+00:00", "payload": meta("episode")},
        {"kind": "run.started", "run_id": "live", "ts": "2026-10-22T00:00:00+00:00", "payload": meta("interactive")},
    ]
    newest = {"quiet-ep": "2026-10-23T10:00:00+00:00",   # 2h quiet, episode: 1h limit
              "live": "2026-10-23T11:00:00+00:00",       # 1h quiet, interactive: 48h
              "archived": "2026-10-20T00:00:00+00:00"}   # 3.5 days, from the table

    def events(**kw):
        if kw.get("kind") == "run.*":
            return page
        return [{"ts": newest[kw["run"]]}] if kw["run"] in newest else []

    monkeypatch.setattr(runlog.pulse_read, "events", events)
    monkeypatch.setattr(runlog, "_db", lambda work, default, **k: [("archived", "interactive", None)])
    assert sorted(runlog._idle_runs_pulse(now)) == ["archived", "quiet-ep"]


def test_sweep_falls_back_to_sql_when_pulse_is_down(monkeypatch):
    monkeypatch.setattr(runlog.pulse_read, "events", lambda **kw: None)
    monkeypatch.setattr(runlog, "_db", lambda work, default, **k: ["sql-closed"])
    with patch.object(runlog, "journal_append") as journal:
        assert runlog.sweep_runs() == ["sql-closed"]
    assert journal.call_args.kwargs["run_id"] == "sql-closed"


def test_interval_strings():
    assert runlog._seconds("1 hour") == 3600
    assert runlog._seconds("48 hours") == 172800
    assert runlog._seconds("30 minutes") == 1800
