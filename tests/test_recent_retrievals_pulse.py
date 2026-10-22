"""get_recent_retrievals reads retrieval.logged events from pulse, falling
back to / topping up from arteries.retrievals. No DB: both sides are stubbed."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from arteries import pulse_read, storage

KEYS = {"prompt_id", "situation", "score", "relevance", "created_at"}


def _ev(ts, project="p1", agent="a1", prompt="x", **extra):
    payload = {"project_id": project, "agent_process_id": agent, "prompt_id": prompt,
               "situation": f"sit-{prompt}", "score": 0.5, **extra}
    return {"kind": "retrieval.logged", "source": "arteries", "ts": ts, "payload": payload}


@pytest.fixture
def table(monkeypatch):
    calls = []
    rows = []

    def fake(project_id, agent_process_id, limit, before=None):
        calls.append((project_id, agent_process_id, limit, before))
        return list(rows)

    monkeypatch.setattr(storage, "_recent_retrievals_table", fake)
    return calls, rows


def _pulse(monkeypatch, events):
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return events

    monkeypatch.setattr(pulse_read, "events", fake)
    return seen


def test_pulse_up_enough_matches(monkeypatch, table):
    calls, _ = table
    seen = _pulse(monkeypatch, [
        _ev("2026-10-22T10:00:05+00:00", prompt="a", relevance=0.7),
        _ev("2026-10-22T10:00:04+00:00", project="other", prompt="no1"),
        _ev("2026-10-22T10:00:03+00:00", agent="other", prompt="no2"),
        _ev("2026-10-22T10:00:02+00:00", prompt="b"),
        _ev("2026-10-22T10:00:01+00:00", prompt="c"),
    ])
    got = storage.get_recent_retrievals("p1", "a1", limit=2)
    assert seen == {"kind": "retrieval.logged", "source": "arteries", "order": "desc", "limit": 200}
    assert calls == []
    assert [r["prompt_id"] for r in got] == ["a", "b"]
    assert all(set(r) == KEYS for r in got)
    assert got[0]["relevance"] == 0.7 and got[1]["relevance"] == 1.0
    assert got[0]["situation"] == "sit-a" and got[0]["score"] == 0.5
    assert isinstance(got[0]["created_at"], datetime)
    assert got[0]["created_at"] == datetime(2026, 10, 22, 10, 0, 5, tzinfo=timezone.utc)


def test_pulse_down_returns_table_rows(monkeypatch, table):
    calls, rows = table
    row = {"prompt_id": "t", "situation": "s", "score": 0.1, "relevance": 1.0,
           "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    rows.append(row)
    _pulse(monkeypatch, None)
    assert storage.get_recent_retrievals("p1", "a1", limit=4) == [row]
    assert calls == [("p1", "a1", 4, None)]


def test_mixed_tops_up_from_table(monkeypatch, table):
    calls, rows = table
    older = [{"prompt_id": f"t{i}", "situation": "s", "score": 0.1, "relevance": 1.0,
              "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)} for i in range(3)]
    rows.extend(older)
    _pulse(monkeypatch, [
        _ev("2026-10-22T10:00:05+00:00", prompt="a"),
        _ev("2026-10-22T10:00:04+00:00", project="other"),
        _ev("2026-10-22T10:00:02+00:00", prompt="b"),
    ])
    got = storage.get_recent_retrievals("p1", "a1", limit=5)
    assert calls == [("p1", "a1", 3, datetime(2026, 10, 22, 10, 0, 2, tzinfo=timezone.utc))]
    assert [r["prompt_id"] for r in got] == ["a", "b", "t0", "t1", "t2"]
    assert all(set(r) == KEYS for r in got)


def test_no_matches_queries_table_unbounded(monkeypatch, table):
    calls, _ = table
    _pulse(monkeypatch, [_ev("2026-10-22T10:00:05+00:00", project="other")])
    assert storage.get_recent_retrievals("p1", "a1", limit=3) == []
    assert calls == [("p1", "a1", 3, None)]


def test_table_error_keeps_pulse_rows(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(storage, "_recent_retrievals_table", boom)
    _pulse(monkeypatch, [_ev("2026-10-22T10:00:05+00:00", prompt="a")])
    assert [r["prompt_id"] for r in storage.get_recent_retrievals("p1", "a1", limit=5)] == ["a"]
    _pulse(monkeypatch, [])
    with pytest.raises(RuntimeError):
        storage.get_recent_retrievals("p1", "a1", limit=5)


def test_naive_ts_becomes_utc(monkeypatch, table):
    _pulse(monkeypatch, [_ev("2026-10-22T10:00:05", prompt="a")])
    got = storage.get_recent_retrievals("p1", "a1", limit=1)
    assert got[0]["created_at"] == datetime(2026, 10, 22, 10, 0, 5, tzinfo=timezone.utc)


class _Cur:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.log.append((" ".join(sql.split()), params))

    def fetchall(self):
        return [{"prompt_id": "t"}]


class _Conn(_Cur):
    def cursor(self, **kw):
        return _Cur(self.log)


def test_table_helper_sql(monkeypatch):
    log = []
    monkeypatch.setattr(storage, "_conn", lambda: _Conn(log))
    cut = datetime(2026, 10, 22, tzinfo=timezone.utc)
    assert storage._recent_retrievals_table("p1", "a1", 4) == [{"prompt_id": "t"}]
    storage._recent_retrievals_table("p1", "a1", 3, before=cut)
    (sql0, params0), (sql1, params1) = log
    assert "created_at <" not in sql0 and params0 == ("p1", "a1", 4)
    assert "AND created_at < %s ORDER BY created_at DESC LIMIT %s" in sql1
    assert params1 == ("p1", "a1", cut, 3)
