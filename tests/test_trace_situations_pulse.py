"""_retrieval_situations reads retrieval.logged events from pulse, filling in
from arteries.retrievals for prompt ids pulse lacks. No DB: both sides stubbed."""
from __future__ import annotations

from datetime import datetime

from arteries import pulse_read, trace


def _ev(seq, ts, prompt, situation="hello world", project="p", agent="a"):
    payload = {"project_id": project, "agent_process_id": agent, "prompt_id": prompt, "situation": situation}
    return {"seq": seq, "kind": "retrieval.logged", "source": "arteries", "ts": ts, "payload": payload}


def _stub(monkeypatch, pages, table=None):
    calls = []

    def fake_events(**kw):
        calls.append(kw)
        return pages[len(calls) - 1] if len(calls) <= len(pages) else []

    monkeypatch.setattr(pulse_read, "events", fake_events)
    monkeypatch.setattr(trace, "_retrieval_situations_table", lambda *a: dict(table or {}))
    return calls


def test_pulse_up(monkeypatch):
    _stub(monkeypatch, [[
        _ev(9, "2026-10-22T10:00:09+00:00", "p1", "newest situation"),
        _ev(8, "2026-10-22T10:00:08+00:00", "p1", "older"),
        _ev(7, "2026-10-22T10:00:07+00:00", "p2", "abc"),
        _ev(6, "2026-10-22T10:00:06+00:00", "x1", project="other"),
        _ev(5, "2026-10-22T10:00:05+00:00", "x2", agent="other"),
    ]], table={"p1": {"situation_preview": "from table"}})
    got = trace._retrieval_situations("p", "a", 5)
    assert set(got) == {"p1", "p2"}
    assert got["p1"]["situation_preview"] == "newes"
    assert got["p1"]["situation_chars"] == len("newest situation")
    assert got["p1"]["situation_truncated"] is True
    assert got["p1"]["created_at"] == datetime.fromisoformat("2026-10-22T10:00:09+00:00")
    assert got["p2"]["situation_preview"] == "abc" and got["p2"]["situation_truncated"] is False


def test_pulse_down(monkeypatch):
    table = {"p1": {"situation_preview": "t"}}
    _stub(monkeypatch, [None], table=table)
    assert trace._retrieval_situations("p", "a", 5) == table


def test_mixed(monkeypatch):
    _stub(monkeypatch, [[_ev(3, "2026-10-22T10:00:03+00:00", "p1", "pulse")]],
          table={"p1": {"situation_preview": "table1"}, "p2": {"situation_preview": "table2"}})
    got = trace._retrieval_situations("p", "a", 10)
    assert got["p1"]["situation_preview"] == "pulse"
    assert got["p2"] == {"situation_preview": "table2"}


def test_paging(monkeypatch):
    calls = _stub(monkeypatch, [
        [_ev(12, "2026-10-22T10:00:12+00:00", "p1"), _ev(10, "2026-10-22T10:00:10+00:00", "p2")],
        [_ev(4, "2026-10-22T10:00:04+00:00", "p3")],
        [],
        [_ev(1, "2026-10-22T10:00:01+00:00", "never")],
    ])
    got = trace._retrieval_situations("p", "a", 50)
    assert len(calls) == 3
    assert calls[0] == {"kind": "retrieval.logged", "source": "arteries", "order": "desc", "limit": 500, "before": None}
    assert calls[1]["before"] == 10 and calls[2]["before"] == 4
    assert set(got) == {"p1", "p2", "p3"}


def test_scan_stops_at_2000_events(monkeypatch):
    full = [[_ev(5000 - i * 500 - j, "2026-10-22T10:00:00+00:00", f"q{i}-{j}") for j in range(500)] for i in range(6)]
    calls = _stub(monkeypatch, full)
    got = trace._retrieval_situations("p", "a", 5)
    assert len(calls) == 4
    assert [c["before"] for c in calls[1:]] == [4501, 4001, 3501]
    assert len(got) == 2000


def test_never_raises(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("down")

    monkeypatch.setattr(pulse_read, "events", boom)
    monkeypatch.setattr(trace, "_retrieval_situations_table", boom)
    assert trace._retrieval_situations("p", "a", 5) == {}
