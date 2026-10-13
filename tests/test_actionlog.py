"""Ledger self-check: decisions with forced JSONL fallback, journal tee,
and runlog episode stamping. No Postgres required."""
from __future__ import annotations

import json
import os
import tempfile
import pytest
import unittest
from pathlib import Path
from unittest import mock

from arteries import actionlog, runlog
from arteries.config import DB_CONFIG


class ActionlogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "repo").mkdir()
        self._env = dict(os.environ)
        os.environ.update({
            "ARTERIES_REPO": str(self.root / "repo"),
            "ARTERIES_PROJECT": "actionlog-selftest",
            "ARTERIES_EPISODE_ID": "ep-selftest-1",
            "ARTERIES_TASK_ID": "task-selftest",
            "EVENT_JOURNAL_DIR": str(self.root / "journal"),
        })
        # force the DB path to fail so the JSONL fallback is what's under test
        self._db = dict(DB_CONFIG)
        DB_CONFIG.update({"host": str(self.root / "no-socket"), "port": 1})

    def tearDown(self):
        DB_CONFIG.clear()
        DB_CONFIG.update(self._db)
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def _journal_events(self) -> list[dict]:
        events = []
        for path in sorted((self.root / "journal").glob("*.ndjson")):
            events += [json.loads(line) for line in path.read_text().splitlines()]
        return events

    def test_decision_fallback_and_journal(self):
        rec = actionlog.log_decision(
            "retrieval.gate",
            chosen_action="abstain",
            available_actions=["abstain", "search"],
            observation={"reason": "test"},
            cost={"confidence": 0.9},
            turn_id="turn-1",
        )
        self.assertEqual(rec["episode_id"], "ep-selftest-1")
        self.assertEqual(rec["metadata"]["task_id"], "task-selftest")

        jsonl = list((self.root / "repo" / ".vascular" / "arteries" / "decisions").glob("*.jsonl"))
        self.assertEqual(len(jsonl), 1)

        rows = actionlog.recent_decisions(episode="ep-selftest-1")
        self.assertEqual(rows[0]["chosen_action"], "abstain")

        journaled = self._journal_events()
        gate = [e for e in journaled if e["kind"] == "decision.retrieval.gate"]
        self.assertEqual(gate[0]["episode_id"], "ep-selftest-1")
        self.assertEqual(gate[0]["payload"]["chosen"], "abstain")
        self.assertEqual(gate[0]["payload"]["store"], "jsonl")  # DB forced down

    @pytest.mark.writes_events

    def test_runlog_stamps_episode_and_tees(self):
        event = runlog.log_event("turn.observed", "arteries", {"message_chars": 12}, turn_id="turn-2")
        self.assertEqual(event["payload"]["episode_id"], "ep-selftest-1")
        observed = [e for e in self._journal_events() if e["kind"] == "turn.observed"]
        self.assertEqual(observed[0]["episode_id"], "ep-selftest-1")
        self.assertEqual(observed[0]["payload"]["message_chars"], 12)
        self.assertEqual(observed[0]["payload"]["store"], "jsonl")
        self.assertEqual(observed[0]["payload"]["project_id"], "actionlog-selftest")
        self.assertEqual(observed[0]["payload"]["repo"], str(self.root / "repo"))
        self.assertIn("cli", observed[0]["payload"])
        self.assertIn("agent_id", observed[0]["payload"])
        self.assertNotIn("episode_id", observed[0]["payload"])

    def test_ingest_heart_episodes(self):
        runs = self.root / "runs"
        (runs / "ep-heart-1").mkdir(parents=True)
        (runs / "ep-heart-1" / "episode.json").write_text(json.dumps({
            "episode_id": "ep-heart-1", "task_id": "toy-1", "outcome": "pass"}))
        with mock.patch.object(actionlog, "_corpus_feedback") as feedback:
            n = actionlog.ingest_heart_episodes(runs)
        self.assertEqual(n, 1)
        self.assertEqual(feedback.call_args.args[0]["outcome"], "pass")

    def test_journal_off(self):
        os.environ["ARTERIES_JOURNAL"] = "off"
        actionlog.log_decision("memory.write_policy", "write_ephemeral",
                               ["write_ephemeral", "discard_ephemeral"])
        self.assertFalse((self.root / "journal").exists())


if __name__ == "__main__":
    unittest.main()


class TestIngestEpisodes(unittest.TestCase):
    def _episodes(self, tmp: Path, records: list[dict]) -> str:
        path = tmp / "episodes.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records))
        return str(path)

    def test_capillaries_hears_every_outcome(self):
        seen = []
        with tempfile.TemporaryDirectory() as d:
            src = self._episodes(Path(d), [
                {"episode_id": "worker-a", "outcome": "unverified"},
                {"episode_id": "merged-b", "outcome": "pass"},
            ])
            with mock.patch.object(actionlog, "_corpus_feedback", side_effect=seen.append), \
                 mock.patch.object(actionlog, "close_episodes"):
                count = actionlog.ingest_episodes(src)
        self.assertEqual(count, 2)
        self.assertEqual([e["episode_id"] for e in seen], ["worker-a", "merged-b"])


class TestIngestClosesEpisodes(unittest.TestCase):
    """arteries.episodes was write-only: 299 of 299 rows read `running`, 287 of
    them older than an hour, `ended_at` never set. The terminal fact was already
    arriving here -- episode.json carries the outcome -- and nothing wrote it
    down."""

    def _episodes(self, tmp: Path, records: list[dict]) -> str:
        path = tmp / "episodes.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records))
        return str(path)

    def _ingest(self, records):
        closed = []
        with tempfile.TemporaryDirectory() as d:
            src = self._episodes(Path(d), records)
            with mock.patch.object(actionlog, "_corpus_feedback"), \
                 mock.patch.object(actionlog, "close_episodes",
                                   side_effect=lambda c: closed.extend(c)):
                actionlog.ingest_episodes(src)
        return closed

    def test_an_episode_is_closed_with_its_outcome(self):
        closed = self._ingest([{"episode_id": "ep-1", "outcome": "pass", "task_id": "t1"}])
        self.assertEqual(closed, [("ep-1", "pass", "t1")])

    def test_a_blocked_episode_still_ended(self):
        closed = self._ingest([{"episode_id": "ep-2", "outcome": "blocked"}])
        self.assertEqual(closed, [("ep-2", "blocked", None)])


class TestEpisodeFacts(unittest.TestCase):
    """An episode's record goes straight to persistent. There is nothing loose
    to compile: episode.json is already the compiled form, and a model call to
    re-say it would add cost, a failure mode at wrap-up, and room to invent."""

    EP = {
        "episode_id": "20260915-102848-a5d32c22",
        "task_id": "work-20260915",
        "repo_path": "/home/me/Projects/arteries",
        "base_commit": "44b2c3b7094450c064b7471f9819533ed3b0d2a6",
        "agent": "claude:haiku",
        "outcome": "fail",
        "diff_lines": 38,
        "verifier_results": {"pytest": {"passed": False}, "ruff": {"passed": True}},
        "scope_refused_paths": ["src/app.py"],
    }

    def test_the_fact_is_mechanical(self):
        line = actionlog.episode_fact(self.EP)
        self.assertIn("fail", line)
        self.assertIn("agent claude:haiku", line)
        self.assertIn("38 diff lines", line)
        self.assertIn("pytest failed", line)
        self.assertIn("ruff passed", line)
        self.assertIn("refused 1 path(s)", line)
        self.assertIn("on arteries", line)      # the repo's name, not its path
        self.assertNotIn("/home/me", line)

    def test_a_row_names_its_episode(self):
        """persistent.episode_id and task_id are columns the table always had
        and nothing ever set."""
        captured = {}
        with mock.patch("arteries.storage.insert_persistent",
                        side_effect=lambda **kw: captured.update(kw) or "row-1"), \
             mock.patch.object(actionlog, "psycopg2") as pg:
            pg.connect.return_value.__enter__.return_value.cursor.return_value \
              .__enter__.return_value.fetchone.return_value = None
            row = actionlog.record_episode(self.EP)
        self.assertEqual(row, "row-1")
        self.assertEqual(captured["episode_id"], self.EP["episode_id"])
        self.assertEqual(captured["task_id"], "work-20260915")
        self.assertEqual(captured["kind"], "episode")
        self.assertIsNone(captured["embedding"], "backfilled, never blocking a wrap-up")

    def test_recording_the_same_episode_twice_writes_one_row(self):
        with mock.patch("arteries.storage.insert_persistent") as insert, \
             mock.patch.object(actionlog, "psycopg2") as pg:
            pg.connect.return_value.__enter__.return_value.cursor.return_value \
              .__enter__.return_value.fetchone.return_value = (1,)
            self.assertIsNone(actionlog.record_episode(self.EP))
        insert.assert_not_called()


def test_a_closed_episode_still_gets_its_record_but_is_not_reposted(monkeypatch, tmp_path):
    """The memory row is written once whatever happened before, but an episode
    already closed is not re-posted to capillaries, which keeps a row per POST."""
    src = tmp_path / "eps.jsonl"
    src.write_text(json.dumps({"episode_id": "old-1", "outcome": "pass"}))
    seen = []
    with mock.patch.object(actionlog, "_corpus_feedback") as feedback, \
         mock.patch.object(actionlog, "close_episodes"), \
         mock.patch.object(actionlog, "record_episode", side_effect=seen.append), \
         mock.patch.object(actionlog, "psycopg2") as pg:
        cur = pg.connect.return_value.__enter__.return_value.cursor.return_value \
                .__enter__.return_value
        cur.fetchall.side_effect = [[("old-1",)], []]   # closed already, never recorded
        count = actionlog.ingest_episodes(str(src))
    assert count == 0
    feedback.assert_not_called()
    assert [e["episode_id"] for e in seen] == ["old-1"], "but the record is written"
