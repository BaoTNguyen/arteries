"""Contract tests for the reward-ledger seam heart depends on
(heart/STACK_READINESS.md sec 1.2 items 4-5):

  4. `art ingest <runs_dir|episodes.jsonl>` backfills arteries.rewards from
     heart's episode.json files, deduped by episode_id.
  5. When Postgres is unreachable, ledger writes degrade to a repo-local
     JSONL fallback under .arteries/decisions/ and the emitted spine event
     records where the write actually landed via a `store` field.

These pin the *shape* of that contract (episode.json fields ingest reads,
what "degraded" looks like on the wire) so a change to either side of the
seam breaks a test here instead of silently drifting.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import psycopg2

from arteries import actionlog
from arteries.config import DB_CONFIG


def _db_reachable() -> bool:
    try:
        conn = psycopg2.connect(connect_timeout=2, **DB_CONFIG)
        conn.close()
        return True
    except Exception:
        return False


DB_REACHABLE = _db_reachable()

# Mirrors heart's src/heart/episode.py episode.json output (confirmed against
# a real run under ~/.local/share/heart/runs), trimmed to the fields
# actionlog.ingest_heart_episodes actually reads: episode_id, task_id,
# outcome, reward.total, reward.components, usage.{tokens_in,tokens_out,cost_usd}.
# Extra fields are kept to prove ingest ignores what it doesn't need.
def _heart_episode(
    episode_id: str, task_id: str, total: float, usage: dict | None = None
) -> dict:
    ep = {
        "episode_id": episode_id,
        "task_id": task_id,
        "prompt": "contract-test prompt, ignored by ingest",
        "repo_path": "/home/bao-tn/Coding/Projects/heart",
        "base_commit": "0" * 40,
        "agent": "claude",
        "outcome": "pass",
        "violations": [],
        "roles": [],
        "verify_rounds": [{"attempt": 0, "passed": True}],
        "review_verdict": "approve",
        "diff_lines": 3,
        "reward": {
            "total": total,
            "components": {"public_tests": 1.0, "diff_quality": 1.0, "efficiency": 0.9},
        },
        "created_at": "2026-07-20T00:00:00+00:00",
    }
    if usage is not None:
        ep["usage"] = usage
    return ep


class IngestRoundTripDedupTests(unittest.TestCase):
    """Test class 1: ingest round-trip + dedup, against whichever backend
    the current host actually exercises (real Postgres if reachable, else
    the documented JSONL fallback)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "repo").mkdir()
        self._env = dict(os.environ)
        os.environ.update({
            "ARTERIES_REPO": str(self.root / "repo"),
            "ARTERIES_PROJECT": "ledger-contract-test",
            "HEART_SPOOL_DIR": str(self.root / "spool"),
        })
        for k in ("ARTERIES_EPISODE_ID", "ARTERIES_TASK_ID"):
            os.environ.pop(k, None)
        self.episode_id = "contract-test-ingest-1"
        self._inserted_episode_ids: set[str] = set()

    def tearDown(self):
        if DB_REACHABLE:
            self._cleanup_db_rows()
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def _cleanup_db_rows(self) -> None:
        try:
            with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
                for eid in self._inserted_episode_ids:
                    cur.execute("DELETE FROM arteries.rewards WHERE episode_id = %s", (eid,))
                    cur.execute("DELETE FROM arteries.decisions WHERE episode_id = %s", (eid,))
                    cur.execute("DELETE FROM arteries.episodes WHERE id = %s", (eid,))
                conn.commit()
        except Exception:
            pass  # best-effort cleanup; nothing else to do if the DB went away mid-test

    def _make_runs_dir(
        self, episode_id: str, usage: dict | None = None, total: float = 0.87
    ) -> Path:
        runs = self.root / "runs"
        ep_dir = runs / episode_id
        ep_dir.mkdir(parents=True)
        (ep_dir / "episode.json").write_text(
            json.dumps(_heart_episode(episode_id, "contract-test-task", total, usage))
        )
        return runs

    @unittest.skipUnless(DB_REACHABLE, "no reachable Postgres; see test_dedup_via_jsonl_fallback")
    def test_ingest_round_trip_and_dedup_via_db(self):
        self._inserted_episode_ids.add(self.episode_id)
        usage = {"tokens_in": 1234, "tokens_out": 567, "cost_usd": 0.0891}
        runs = self._make_runs_dir(self.episode_id, usage=usage)

        n1 = actionlog.ingest_heart_episodes(runs)
        self.assertEqual(n1, 1)

        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT value, tokens_in, tokens_out, cost_usd FROM arteries.rewards "
                "WHERE episode_id = %s AND reward_type = 'episode'",
                (self.episode_id,),
            )
            rows = cur.fetchall()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(float(rows[0][0]), 0.87)
        self.assertEqual(rows[0][1], 1234)
        self.assertEqual(rows[0][2], 567)
        self.assertAlmostEqual(float(rows[0][3]), 0.0891)

        # second ingest of the same runs dir must not duplicate the row
        n2 = actionlog.ingest_heart_episodes(runs)
        self.assertEqual(n2, 0)

        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM arteries.rewards WHERE episode_id = %s AND reward_type = 'episode'",
                (self.episode_id,),
            )
            count = cur.fetchone()[0]
        self.assertEqual(count, 1)

    @unittest.skipUnless(DB_REACHABLE, "no reachable Postgres; see jsonl-fallback tests")
    def test_ingest_without_usage_field_still_ingests_with_nulls(self):
        """Older episode.json files (written before heart added the `usage`
        object) must still ingest cleanly, with the three new columns null."""
        episode_id = "contract-test-ingest-no-usage-1"
        self._inserted_episode_ids.add(episode_id)
        runs = self._make_runs_dir(episode_id, usage=None, total=0.42)

        n1 = actionlog.ingest_heart_episodes(runs)
        self.assertEqual(n1, 1)

        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT value, tokens_in, tokens_out, cost_usd FROM arteries.rewards "
                "WHERE episode_id = %s AND reward_type = 'episode'",
                (episode_id,),
            )
            rows = cur.fetchall()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(float(rows[0][0]), 0.42)
        self.assertIsNone(rows[0][1])
        self.assertIsNone(rows[0][2])
        self.assertIsNone(rows[0][3])

    @unittest.skipIf(DB_REACHABLE, "Postgres is reachable on this host; DB path covered above")
    def test_ingest_round_trip_via_jsonl_fallback(self):
        """No DB on this host: ingest still runs, but the documented fallback
        applies -- writes land in .arteries/decisions/ and dedup does NOT
        happen across runs (actionlog._persist explicitly gives up on
        cross-run dedup in jsonl-fallback mode; see the `# ponytail:` note
        in ingest_heart_episodes)."""
        usage = {"tokens_in": 111, "tokens_out": 222, "cost_usd": 0.0033}
        runs = self._make_runs_dir(self.episode_id, usage=usage)

        n1 = actionlog.ingest_heart_episodes(runs)
        self.assertEqual(n1, 1)

        jsonl_dir = self.root / "repo" / ".arteries" / "decisions"
        records = [
            json.loads(line)
            for p in jsonl_dir.glob("*.jsonl")
            for line in p.read_text().splitlines()
        ]
        rewards = [r for r in records if r.get("kind") == "reward" and r.get("episode_id") == self.episode_id]
        self.assertEqual(len(rewards), 1)
        self.assertAlmostEqual(rewards[0]["value"], 0.87)
        self.assertEqual(rewards[0]["components"]["outcome"], "pass")
        self.assertEqual(rewards[0]["tokens_in"], 111)
        self.assertEqual(rewards[0]["tokens_out"], 222)
        self.assertAlmostEqual(rewards[0]["cost_usd"], 0.0033)

        # documented behavior: jsonl-fallback mode has no cross-run dedup,
        # so a second ingest of the same file re-appends.
        n2 = actionlog.ingest_heart_episodes(runs)
        self.assertEqual(n2, 1)
        records2 = [
            json.loads(line)
            for p in jsonl_dir.glob("*.jsonl")
            for line in p.read_text().splitlines()
        ]
        rewards2 = [r for r in records2 if r.get("kind") == "reward" and r.get("episode_id") == self.episode_id]
        self.assertEqual(len(rewards2), 2)


class DegradationDrillTests(unittest.TestCase):
    """Test class 2: point the DB at a dead port, trigger a ledger write
    through actionlog's public API, and assert the failure is contained --
    no exception escapes, the JSONL fallback file appears, and the spine
    event's `store` field pins which path actually fired."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "repo").mkdir()
        self._env = dict(os.environ)
        os.environ.update({
            "ARTERIES_REPO": str(self.root / "repo"),
            "ARTERIES_PROJECT": "ledger-degradation-test",
            "ARTERIES_EPISODE_ID": "contract-test-degradation-1",
            "ARTERIES_TASK_ID": "contract-test-task",
            "HEART_SPOOL_DIR": str(self.root / "spool"),
            # Documented for operators, but DB_CONFIG is a module-level dict
            # already resolved from env at import time (see arteries/config.py)
            # -- setting env here alone would be a no-op for a live process.
            # We set them anyway for honesty/documentation, then force the
            # dict directly the same way arteries' own test_actionlog.py does.
            "DB_HOST": "127.0.0.1",
            "DB_PORT": "1",
        })
        self._db = dict(DB_CONFIG)
        DB_CONFIG.update({"host": "127.0.0.1", "port": 1})

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

    def test_dead_db_degrades_to_jsonl_and_spool_records_store(self):
        try:
            rec = actionlog.log_decision(
                "contract.degradation",
                chosen_action="fallback",
                available_actions=["fallback", "raise"],
                observation={"reason": "dead port drill"},
            )
        except Exception as exc:  # pragma: no cover - the point of this test
            self.fail(f"log_decision raised instead of degrading gracefully: {exc!r}")

        self.assertEqual(rec["episode_id"], "contract-test-degradation-1")

        jsonl_files = list((self.root / "repo" / ".arteries" / "decisions").glob("*.jsonl"))
        self.assertEqual(len(jsonl_files), 1, "expected exactly one JSONL fallback file")

        jsonl_records = [json.loads(line) for line in jsonl_files[0].read_text().splitlines()]
        matching = [r for r in jsonl_records if r.get("id") == rec["id"]]
        self.assertEqual(len(matching), 1)

        spooled = self._spool_events()
        gate = [e for e in spooled if e["kind"] == "decision.contract.degradation"]
        self.assertEqual(len(gate), 1)
        # Pin reality: with a dead DB but a writable tempdir, the jsonl write
        # succeeds, so the emitted store flag is "jsonl" (not "lost"). "lost"
        # only fires if the jsonl write itself also raises.
        self.assertEqual(gate[0]["payload"]["store"], "jsonl")


if __name__ == "__main__":
    unittest.main()
