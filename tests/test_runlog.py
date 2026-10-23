import json
import os
import tempfile
import uuid
import psycopg2
import pytest
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from arteries import runlog
from arteries.config import DB_CONFIG

# This module is about the runlog write path; the conftest guard would
# stub out the very thing under test.
pytestmark = pytest.mark.writes_events


from dbprobe import DB_REACHABLE


class RunlogTests(unittest.TestCase):
    def setUp(self):
        # the fallback anchors on ARTERIES_REPO before cwd; heart sets it for
        # every agent, so these tests wrote at the host repo path instead of tmp
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("ARTERIES_REPO", None)

    def test_log_event_journals_with_the_db_down(self):
        # pulse P5 (A4): the journal line is the record; agent_events is its
        # projection and the drain backfills it, so no repo JSONL fallback
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {"EVENT_JOURNAL_DIR": tmp, "VASCULAR_HOME": tmp}), \
                patch.object(runlog.psycopg2, "connect", side_effect=RuntimeError("db down")), \
                patch.object(runlog, "_write_current_run"):
            os.environ.pop("ARTERIES_JOURNAL", None)
            runlog._resolved.clear()  # a run cached by an earlier test keeps its project
            event = runlog.log_event("turn.observed", "arteries",
                                     {"not_json": Mock(name="not-json")}, project_id="test-project")
            lines = [json.loads(l) for f in Path(tmp).glob("*.ndjson")
                     for l in f.read_text().splitlines()]
        row = next(l for l in lines if l["kind"] == "turn.observed")
        self.assertEqual(row["id"], event["id"])
        self.assertEqual(row["payload"]["project_id"], "test-project")
        self.assertEqual(row["payload"]["store"], "pulse")
        self.assertIn("not-json", row["payload"]["not_json"])
        self.assertFalse((Path(tmp) / ".vascular").exists())


class RunlogDiscardTests(unittest.TestCase):
    """ARTERIES_RUNLOG=discard is what makes the smoke script's dry run dry.
    Without it, a run advertised as a dry run wrote turn.observed rows into the
    live store and repointed current-run at a run created by a test."""

    def test_discard_writes_nothing(self):
        with patch.dict(os.environ, {"ARTERIES_RUNLOG": "discard"}), \
                patch.object(runlog, "_write_db_run") as db_run, \
                patch.object(runlog.projection, "project") as project, \
                patch.object(runlog, "journal_append") as journal:
            event = runlog.log_event("turn.observed", "test", {"message_preview": "hi"})

        db_run.assert_not_called()
        project.assert_not_called()
        journal.assert_not_called()
        self.assertEqual(event["event_type"], "turn.observed")

    def test_discard_leaves_the_current_run_pointer_alone(self):
        with patch.dict(os.environ, {"ARTERIES_RUNLOG": "discard"}), \
                patch.object(runlog, "_current_run_path") as path:
            runlog._write_current_run(Path("/nonexistent"), {"run_id": "r", "cli": "claude"})

        path.assert_not_called()

    def test_without_discard_the_line_is_journaled_and_projected(self):
        env = {k: v for k, v in os.environ.items() if k != "ARTERIES_RUNLOG"}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(runlog, "_write_db_run"), \
                patch.object(runlog, "_write_current_run"), \
                patch.object(runlog, "journal_append", return_value={"id": "x"}) as journal, \
                patch.object(runlog.projection, "project") as project:
            runlog.log_event("turn.observed", "test", {})

        journal.assert_called_once()
        project.assert_called_once_with({"id": "x"})

    def test_session_id_is_stamped_from_the_environment(self):
        # the finer key the packet filters on -- without it, concurrent sessions
        # in one repo share a run and get merged into one conversation
        with patch.dict(os.environ, {"ARTERIES_RUNLOG": "discard",
                                     "ARTERIES_SESSION_ID": "sess-42"}):
            event = runlog.log_event("turn.observed", "test", {})

        self.assertEqual(event["payload"]["session_id"], "sess-42")


@unittest.skipUnless(DB_REACHABLE, "no reachable Postgres; run lifecycle is a database contract")
class RunLifecycleTests(unittest.TestCase):
    """Runs are keyed by session and closed by a sweep.

    Before this, the pointer was keyed by CLI, so "resume my session" meant
    "join whatever ran last under that name", and nothing ever wrote ended_at --
    32 runs, 0 ever ended, one of them spanning six days of unrelated work.
    """

    def setUp(self):
        self.project = "zz-test-" + uuid.uuid4().hex[:10]
        self.repo = "/tmp/" + self.project
        self.env = patch.dict(os.environ, {
            "ARTERIES_PROJECT": self.project, "ARTERIES_REPO": self.repo,
            "ARTERIES_CLI": "claude", "ARTERIES_AGENT_ID": "test",
        })
        self.env.start()
        os.environ.pop("ARTERIES_RUN_ID", None)
        os.environ.pop("AGENT_RUN_ID", None)
        os.environ.pop("ARTERIES_EPISODE_ID", None)
        runlog._resolved.clear()

    def tearDown(self):
        self.env.stop()
        runlog._resolved.clear()
        try:
            with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM arteries.agent_events WHERE project_id = %s", (self.project,))
                cur.execute("DELETE FROM arteries.agent_runs WHERE project_id = %s", (self.project,))
        except Exception:
            pass

    def _begin(self, session=None):
        """Start a session the way a session-start hook does."""
        self._session(session)
        return runlog.start_run(project_id=self.project, agent_id="test",
                                cli="claude", repo_path=self.repo)

    def _run(self, session=None):
        """Resolve the current run the way a mid-session hook does."""
        self._session(session)
        return runlog.current_run(project_id=self.project, cli="claude", repo_path=self.repo)

    def _backdate(self, run_id, interval):
        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE arteries.agent_runs SET started_at = now() - interval '{interval}' "
                        "WHERE id = %s", (run_id,))
            cur.execute(f"UPDATE arteries.agent_events SET created_at = now() - interval '{interval}' "
                        "WHERE run_id = %s", (run_id,))

    def _session(self, session):
        runlog._resolved.clear()
        if session is None:
            os.environ.pop("ARTERIES_SESSION_ID", None)
        else:
            os.environ["ARTERIES_SESSION_ID"] = session

    def test_same_session_resumes_the_same_run(self):
        first = self._begin("sess-A")
        self.assertEqual(self._run("sess-A")["run_id"], first["run_id"])
        self.assertEqual(first["metadata"]["session_id"], "sess-A")
        self.assertEqual(first["metadata"]["kind"], "interactive")

    def test_a_closed_run_is_never_rejoined_and_links_back(self):
        first = self._begin("sess-A")
        self.assertTrue(runlog.end_run(first["run_id"], "episode_finished"))
        second = self._begin("sess-A")
        self.assertNotEqual(second["run_id"], first["run_id"])
        self.assertEqual(second["metadata"]["previous_run_id"], first["run_id"])

    def test_a_new_session_does_not_inherit_the_per_cli_pointer(self):
        """The bug the ladder exists to stop: the per-CLI file names whatever
        ran last, so a fresh session used to be handed the previous one's run."""
        a = self._begin("sess-A")
        b = self._begin("sess-B")
        self.assertNotEqual(b["run_id"], a["run_id"])
        self.assertEqual(b["metadata"]["session_id"], "sess-B")

    def test_an_assigned_run_id_wins_outright(self):
        assigned = str(uuid.uuid4())
        with patch.dict(os.environ, {"ARTERIES_RUN_ID": assigned,
                                     "ARTERIES_EPISODE_ID": "ep-1"}):
            runlog._resolved.clear()
            run = runlog.current_run(project_id=self.project, cli="claude", repo_path=self.repo)
        self.assertEqual(run["run_id"], assigned)
        self.assertEqual(run["metadata"]["kind"], "episode")

    def test_end_run_is_idempotent(self):
        run = self._begin("sess-A")
        self.assertTrue(runlog.end_run(run["run_id"]))
        self.assertFalse(runlog.end_run(run["run_id"]))

    def test_sweep_closes_an_idle_run_and_spares_a_live_one(self):
        stale = self._begin("sess-stale")
        fresh = self._begin("sess-fresh")
        # idleness is measured from the last event, not from started_at, so a
        # run with a fresh run.started row is live no matter how old it looks
        self._backdate(stale["run_id"], "60 hours")

        closed = runlog.sweep_runs()

        self.assertIn(stale["run_id"], closed)
        self.assertNotIn(fresh["run_id"], closed)

    def test_sweep_closes_a_dead_episode_far_sooner(self):
        with patch.dict(os.environ, {"ARTERIES_EPISODE_ID": "ep-1"}):
            runlog._resolved.clear()
            episode = runlog.start_run(project_id=self.project, agent_id="test",
                                       cli="claude", repo_path=self.repo)
        self._backdate(episode["run_id"], "2 hours")

        # two hours is nothing for an interactive run and terminal for an episode
        self.assertIn(episode["run_id"], runlog.sweep_runs())


if __name__ == "__main__":
    unittest.main()
