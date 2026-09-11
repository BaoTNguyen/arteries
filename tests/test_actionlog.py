"""Ledger self-check: decisions/rewards with forced JSONL fallback, journal tee,
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

        jsonl = list((self.root / "repo" / ".arteries" / "decisions").glob("*.jsonl"))
        self.assertEqual(len(jsonl), 1)

        rows = actionlog.recent_decisions(episode="ep-selftest-1")
        self.assertEqual(rows[0]["chosen_action"], "abstain")

        journaled = self._journal_events()
        gate = [e for e in journaled if e["kind"] == "decision.retrieval.gate"]
        self.assertEqual(gate[0]["episode_id"], "ep-selftest-1")
        self.assertEqual(gate[0]["payload"]["chosen"], "abstain")
        self.assertEqual(gate[0]["payload"]["store"], "jsonl")  # DB forced down

    def test_reward_fallback_and_journal(self):
        actionlog.log_reward("task_success", 0.85, components={"public_tests": 1.0}, source="heart")
        kinds = [e["kind"] for e in self._journal_events()]
        self.assertIn("reward.task_success", kinds)

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
            "episode_id": "ep-heart-1", "task_id": "toy-1", "outcome": "pass",
            "reward": {"total": 0.9, "components": {"public_tests": 1.0}},
        }))
        n = actionlog.ingest_heart_episodes(runs)
        self.assertEqual(n, 1)

        records = [
            json.loads(line)
            for p in (self.root / "repo" / ".arteries" / "decisions").glob("*.jsonl")
            for line in p.read_text().splitlines()
        ]
        rewards = [r for r in records if r["kind"] == "reward"]
        self.assertEqual(rewards[0]["episode_id"], "ep-heart-1")
        self.assertEqual(rewards[0]["value"], 0.9)
        self.assertEqual(rewards[0]["components"]["outcome"], "pass")
        # env identity restored after the ingest loop
        self.assertEqual(os.environ["ARTERIES_EPISODE_ID"], "ep-selftest-1")
        self.assertIn("reward.episode", [e["kind"] for e in self._journal_events()])

    def test_journal_off(self):
        os.environ["ARTERIES_JOURNAL"] = "off"
        actionlog.log_decision("memory.write_policy", "write_ephemeral",
                               ["write_ephemeral", "discard_ephemeral"])
        self.assertFalse((self.root / "journal").exists())


if __name__ == "__main__":
    unittest.main()


class TestIngestUnscoredEpisodes(unittest.TestCase):
    """A null reward means nothing measured the episode. Scoring it 0.0 would
    invent a failure; raising on it used to abort the whole ingest, which left
    every later episode in the directory permanently unread."""

    def _episodes(self, tmp: Path, records: list[dict]) -> str:
        path = tmp / "episodes.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records))
        return str(path)

    def test_a_null_reward_is_skipped_not_scored_zero(self):
        logged = []
        with tempfile.TemporaryDirectory() as d:
            src = self._episodes(Path(d), [
                {"episode_id": "worker-a", "outcome": "unverified",
                 "reward": {"total": None, "components": {}}},
                {"episode_id": "merged-b", "outcome": "pass",
                 "reward": {"total": 0.99, "components": {"public_tests": 1.0}}},
            ])
            with mock.patch.object(actionlog, "log_reward",
                                   side_effect=lambda *a, **k: logged.append((a, k))), \
                 mock.patch.object(actionlog, "_corpus_feedback"), \
                 mock.patch.object(actionlog, "journal_append"):
                count = actionlog.ingest_episodes(src)
        self.assertEqual(count, 1)                      # only the scored one
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0][0][1], 0.99)         # and it kept its value

    def test_one_unscored_episode_does_not_strand_the_rest(self):
        # the null used to raise inside the loop, so everything sorted after it
        # was never read -- permanently, because the retry hit the same record.
        logged = []
        with tempfile.TemporaryDirectory() as d:
            src = self._episodes(Path(d), [
                {"episode_id": "first", "outcome": "unverified", "reward": {"total": None}},
                {"episode_id": "second", "outcome": "pass", "reward": {"total": 0.5}},
                {"episode_id": "third", "outcome": "fail", "reward": {"total": 0.0}},
            ])
            with mock.patch.object(actionlog, "log_reward",
                                   side_effect=lambda *a, **k: logged.append(a[1])), \
                 mock.patch.object(actionlog, "_corpus_feedback"), \
                 mock.patch.object(actionlog, "journal_append"):
                count = actionlog.ingest_episodes(src)
        self.assertEqual(count, 2)
        self.assertEqual(logged, [0.5, 0.0])            # a real 0.0 still lands

    def test_capillaries_still_hears_the_outcome_of_an_unscored_episode(self):
        seen = []
        with tempfile.TemporaryDirectory() as d:
            src = self._episodes(Path(d), [
                {"episode_id": "worker-a", "outcome": "unverified", "reward": {"total": None}},
            ])
            with mock.patch.object(actionlog, "log_reward"), \
                 mock.patch.object(actionlog, "_corpus_feedback", side_effect=seen.append), \
                 mock.patch.object(actionlog, "journal_append"):
                actionlog.ingest_episodes(src)
        self.assertEqual([e["episode_id"] for e in seen], ["worker-a"])
