import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arteries import trace


class TraceTests(unittest.TestCase):
    def test_trace_infers_project_agent_and_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            config_dir = repo / ".arteries"
            config_dir.mkdir()
            (config_dir / "config.json").write_text(
                json.dumps({
                    "project": "career-ops",
                    "agent_id": "career-ops-hook",
                    "cli": "codex",
                }) + "\n",
                encoding="utf-8",
            )
            (config_dir / "current-run.json").write_text(
                json.dumps({
                    "run_id": "run-1",
                    "project_id": "career-ops",
                    "agent_id": "career-ops-hook",
                    "cli": "codex",
                }) + "\n",
                encoding="utf-8",
            )

            events = [
                {
                    "event_type": "turn.observed",
                    "turn_id": "next",
                    "created_at": "2026-01-01T00:03:00Z",
                    "payload": {"message_preview": "after", "message_chars": 5, "message_sha256": "h3"},
                },
                {
                    "event_type": "prompt.retrieved",
                    "turn_id": "invoke",
                    "created_at": "2026-01-01T00:02:30Z",
                    "payload": {"prompt_id": "p1", "confidence": 0.9},
                },
                {
                    "event_type": "prompt.gate.decided",
                    "turn_id": "invoke",
                    "created_at": "2026-01-01T00:02:00Z",
                    "payload": {"search": True, "reason": "corpus match found (similarity=0.5, closest=Gate Title)"},
                },
                {
                    "event_type": "turn.observed",
                    "turn_id": "invoke",
                    "created_at": "2026-01-01T00:01:00Z",
                    "payload": {"message_preview": "during", "message_chars": 6, "message_sha256": "h2"},
                },
                {
                    "event_type": "turn.observed",
                    "turn_id": "prev",
                    "created_at": "2026-01-01T00:00:00Z",
                    "payload": {"message_preview": "before", "message_chars": 6, "message_sha256": "h1"},
                },
            ]

            with patch.object(trace.runlog, "summarize", return_value={"event_count": 1}) as summarize, \
                 patch.object(trace.runlog, "recent_events", return_value=events) as recent, \
                 patch.object(trace, "_prompt_refs", return_value={"p1": {"title": "Prompt One"}}) as prompt_refs, \
                 patch.object(trace, "_retrieval_situations", return_value={"p1": {"situation_preview": "full invoke", "situation_chars": 11, "situation_truncated": False}}) as situations, \
                 patch.object(trace.storage, "get_ephemeral", return_value=[]) as ephemeral, \
                 patch.object(trace.storage, "get_persistent", return_value=[]) as persistent, \
                 patch("builtins.print") as print_:
                self.assertEqual(trace.main(["--repo", str(repo), "--events", "5", "--memories", "3"]), 0)

            payload = json.loads(print_.call_args.args[0])
            self.assertEqual(payload["project_id"], "career-ops")
            self.assertEqual(payload["agent_id"], "career-ops-hook")
            self.assertEqual(payload["configured_cli"], "codex")
            self.assertEqual(payload["prompt_timeline"][0]["kind"], "gate_decision")
            self.assertEqual(payload["prompt_timeline"][0]["gate_nearest_match_title"], "Gate Title")
            self.assertEqual(payload["prompt_timeline"][1]["kind"], "prompt_retrieved")
            self.assertEqual(payload["prompt_timeline"][1]["retrieved_prompt_id"], "p1")
            self.assertEqual(payload["prompt_timeline"][1]["retrieved_prompt"]["title"], "Prompt One")
            context = payload["prompt_timeline"][1]["message_context"]
            self.assertEqual(context["previous_observed_user_turn"]["message_preview"], "before")
            self.assertEqual(context["invocation_user_turn"]["message_preview"], "during")
            self.assertEqual(context["invocation_user_turn"]["retrieval_situation_preview"], "full invoke")
            self.assertEqual(context["next_observed_user_turn"]["message_preview"], "after")
            prompt_refs.assert_called_once_with(["p1"], 500)
            situations.assert_called_once_with("career-ops", "career-ops-hook", 500)
            summarize.assert_called_once_with("career-ops", limit=5, repo_path=repo.resolve())
            recent.assert_called_once_with("career-ops", limit=5, repo_path=repo.resolve())
            ephemeral.assert_called_once_with("career-ops", "career-ops-hook", limit=3)
            persistent.assert_called_once_with("career-ops", limit=3)


if __name__ == "__main__":
    unittest.main()
