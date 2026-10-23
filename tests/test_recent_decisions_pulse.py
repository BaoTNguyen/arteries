"""recent_decisions reads full-record decision.* events from pulse first and
tops up from arteries.decisions with older rows only (pulse P5, A3)."""

from arteries import actionlog, pulse_read


def _event(i, project="p", episode=None, full=True):
    payload = {"chosen": f"c{i}", "available": ["a", f"c{i}"], "store": "db"}
    if full:
        payload.update(project_id=project, observation={"n": i}, metadata={},
                       created_at=f"2026-10-23T00:00:0{i}+00:00")
    return {"id": f"id{i}", "kind": "decision.retrieval.gate", "episode_id": episode,
            "run_id": None, "turn_id": None, "payload": payload}


def test_pulse_rows_first_then_older_table_rows(monkeypatch):
    calls = {}
    monkeypatch.setattr(pulse_read, "events", lambda **kw: (calls.update(kw), [
        _event(3), _event(2, project="other"), _event(1, full=False)])[1])

    def table(project, episode, limit, before=None):
        calls["table"] = (project, episode, limit, before)
        return [{"id": "old", "created_at": "2026-10-22T00:00:00+00:00"}]

    monkeypatch.setattr(actionlog, "_recent_decisions_table", table)
    rows = actionlog.recent_decisions(project_id="p", limit=5)
    assert calls["kind"] == "decision.*" and calls["order"] == "desc"
    assert [r["id"] for r in rows] == ["id3", "old"]
    assert rows[0]["decision_type"] == "retrieval.gate"
    assert rows[0]["chosen_action"] == "c3" and rows[0]["observation"] == {"n": 3}
    assert calls["table"] == ("p", None, 4, "2026-10-23T00:00:03+00:00")


def test_episode_filter_uses_the_envelope(monkeypatch):
    monkeypatch.setattr(pulse_read, "events", lambda **kw: [
        _event(2, episode="ep-x"), _event(1, episode="ep-y")])
    rows = actionlog.recent_decisions(episode="ep-y", limit=1)
    assert [r["id"] for r in rows] == ["id1"]


def test_pulse_down_falls_back_to_the_table(monkeypatch):
    monkeypatch.setattr(pulse_read, "events", lambda **kw: None)
    monkeypatch.setattr(actionlog, "_recent_decisions_table",
                        lambda *a, **k: [{"id": "t1"}])
    assert actionlog.recent_decisions(project_id="p") == [{"id": "t1"}]
