"""Ledger self-check: decisions/rewards with forced JSONL fallback, spool tee,
and runlog episode stamping. No Postgres required."""
from __future__ import annotations

import json
import os
import tempfile
import pytest
import unittest
from pathlib import Path

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
            "HEART_SPOOL_DIR": str(self.root / "spool"),
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

    def _spool_events(self) -> list[dict]:
        events = []
        for path in sorted((self.root / "spool").glob("*.ndjson")):
            events += [json.loads(line) for line in path.read_text().splitlines()]
        return events

    def test_decision_fallback_and_spool(self):
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

        spooled = self._spool_events()
        gate = [e for e in spooled if e["kind"] == "decision.retrieval.gate"]
        self.assertEqual(gate[0]["episode_id"], "ep-selftest-1")
        self.assertEqual(gate[0]["payload"]["chosen"], "abstain")
        self.assertEqual(gate[0]["payload"]["store"], "jsonl")  # DB forced down

    def test_reward_fallback_and_spool(self):
        actionlog.log_reward("task_success", 0.85, components={"public_tests": 1.0}, source="heart")
        kinds = [e["kind"] for e in self._spool_events()]
        self.assertIn("reward.task_success", kinds)

    @pytest.mark.writes_events

    def test_runlog_stamps_episode_and_tees(self):
        event = runlog.log_event("turn.observed", "arteries", {"message_chars": 12}, turn_id="turn-2")
        self.assertEqual(event["payload"]["episode_id"], "ep-selftest-1")
        observed = [e for e in self._spool_events() if e["kind"] == "turn.observed"]
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
        self.assertIn("reward.episode", [e["kind"] for e in self._spool_events()])

    def test_spool_off(self):
        os.environ["ARTERIES_SPOOL"] = "off"
        actionlog.log_decision("memory.write_policy", "write_ephemeral",
                               ["write_ephemeral", "discard_ephemeral"])
        self.assertFalse((self.root / "spool").exists())


if __name__ == "__main__":
    unittest.main()
