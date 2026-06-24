import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from arteries import runlog


class RunlogTests(unittest.TestCase):
    def test_log_event_falls_back_to_repo_jsonl(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                with patch.object(runlog.psycopg2, "connect", side_effect=RuntimeError("db down")):
                    event = runlog.log_event(
                        "turn.observed",
                        "arteries",
                        {"not_json": Mock(name="not-json")},
                        project_id="test-project",
                    )

                path = Path(".arteries") / "runs" / f"{event['run_id']}.jsonl"
                self.assertTrue(path.exists())
                row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
                self.assertEqual(row["event_type"], "turn.observed")
                self.assertEqual(row["project_id"], "test-project")
                self.assertIn("not-json", row["payload"]["not_json"])
            finally:
                os.chdir(old_cwd)

    def test_start_run_and_summary_use_repo_fallback(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                with patch.object(runlog.psycopg2, "connect", side_effect=RuntimeError("db down")):
                    run = runlog.start_run(project_id="test-project", agent_id="agent-1", cli="codex")
                    runlog.log_event(
                        "memory.ephemeral.extracted",
                        "arteries",
                        {"count": 2},
                        project_id="test-project",
                    )
                    summary = runlog.summarize("test-project", repo_path=tmp)

                current = json.loads(Path(".arteries/current-run.json").read_text(encoding="utf-8"))
                self.assertEqual(current["run_id"], run["run_id"])
                self.assertEqual(current["cli"], "codex")
                self.assertEqual(summary["latest_run_id"], run["run_id"])
                self.assertEqual(summary["extracted_total"], 2)
                self.assertEqual(summary["counts_by_type"]["run.started"], 1)
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
