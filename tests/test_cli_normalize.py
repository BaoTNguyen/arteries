import json
import subprocess
import sys
import unittest

from arteries.cli_normalize import normalize


class CliNormalizeTests(unittest.TestCase):
    def test_claude_subagent_event_gets_parent_and_stable_child_id(self):
        event = normalize(
            {
                "hook_event_name": "SubagentStart",
                "session_id": "s1",
                "cwd": "/repo",
                "subagent_type": "Explore",
            },
            cli="claude",
            project_id="career-ops",
            agent_id="career-ops-hook",
        )

        self.assertEqual(event.event, "subagent_start")
        self.assertEqual(event.agent_role, "subagent")
        self.assertEqual(event.parent_agent_id, "career-ops-hook")
        self.assertTrue(event.agent_id.startswith("career-ops-hook:claude:explore:"))
        self.assertEqual(event.session_id, "s1")
        self.assertEqual(event.cwd, "/repo")

    def test_codex_precompact_normalizes_to_compact(self):
        event = normalize(
            {"hookEventName": "PreCompact", "trigger": "auto"},
            cli="codex",
            project_id="demo",
            agent_id="demo-hook",
        )

        self.assertEqual(event.event, "compact")
        self.assertEqual(event.agent_role, "parent")
        self.assertEqual(event.agent_id, "demo-hook")

    def test_pi_session_before_compact_fallback_normalizes_to_compact(self):
        event = normalize(
            {"reason": "threshold", "preparation": {"firstKeptEntryId": "e1"}},
            cli="pi",
            fallback_event="session_before_compact",
            project_id="demo",
            agent_id="demo-hook",
        )

        self.assertEqual(event.event, "compact")
        self.assertEqual(event.cli, "pi")
        self.assertEqual(event.agent_role, "parent")

    def test_claude_session_start_compact_source_normalizes_to_compact(self):
        """Claude Code fires one hook_event_name (SessionStart) for four
        matchers and tells them apart with `source` -- the field
        hooks.json's own matcher string ("startup|resume|clear|compact") is
        written against. Without reading it, the compact-specific SessionStart
        hook (setup_cli.py's `_claude_hooks`) canonicalizes identically to
        plain startup, and planning/compaction_v3.md's renderer split never
        fires for the CLI most sessions actually run."""
        event = normalize(
            {"hook_event_name": "SessionStart", "source": "compact",
             "session_id": "s1"},
            cli="claude",
            project_id="demo",
            agent_id="demo-hook",
        )

        self.assertEqual(event.event, "compact")

    def test_claude_session_start_startup_source_is_unaffected(self):
        for source in ("startup", "resume", "clear"):
            event = normalize(
                {"hook_event_name": "SessionStart", "source": source},
                cli="claude", project_id="demo", agent_id="demo-hook",
            )
            self.assertEqual(event.event, "session_start")

    def test_hermes_common_subagent_fields_are_supported_conservatively(self):
        event = normalize(
            {"event": "subagent_start", "agent_id": "child", "parent_agent_id": "parent"},
            cli="hermes",
            project_id="demo",
        )

        self.assertEqual(event.event, "subagent_start")
        self.assertEqual(event.agent_role, "subagent")
        self.assertEqual(event.agent_id, "child")
        self.assertEqual(event.parent_agent_id, "parent")


    def test_assistant_response_events_normalize_consistently(self):
        event = normalize(
            {"hookEventName": "AssistantResponse", "transcript_path": "/tmp/transcript.jsonl"},
            cli="claude",
            project_id="demo",
            agent_id="demo-hook",
        )

        self.assertEqual(event.event, "assistant_response")
        self.assertEqual(event.agent_role, "parent")
        self.assertEqual(event.agent_id, "demo-hook")

    def test_transcript_field_cli(self):
        payload = json.dumps({"event": "UserPromptSubmit", "transcript_path": "/tmp/transcript.jsonl"})
        transcript = subprocess.check_output(
            [
                sys.executable,
                "-m",
                "arteries.cli_normalize",
                "--cli",
                "claude",
                "--field",
                "transcript",
            ],
            input=payload,
            text=True,
        )

        self.assertEqual(transcript.strip(), "/tmp/transcript.jsonl")

    def test_shell_exports_and_message_field_cli(self):
        payload = json.dumps({"event": "UserPromptSubmit", "prompt": "Build it"})
        shell = subprocess.check_output(
            [
                sys.executable,
                "-m",
                "arteries.cli_normalize",
                "--cli",
                "claude",
                "--project",
                "demo",
                "--agent",
                "demo-hook",
                "--format",
                "shell",
            ],
            input=payload,
            text=True,
        )
        message = subprocess.check_output(
            [
                sys.executable,
                "-m",
                "arteries.cli_normalize",
                "--cli",
                "claude",
                "--field",
                "message",
            ],
            input=payload,
            text=True,
        )

        self.assertIn("export ARTERIES_EVENT=prompt", shell)
        self.assertIn("export ARTERIES_AGENT_ID=demo-hook", shell)
        self.assertEqual(message.strip(), "Build it")


if __name__ == "__main__":
    unittest.main()
