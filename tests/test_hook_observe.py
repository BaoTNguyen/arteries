import os
import unittest
from unittest.mock import patch

from arteries import hook_observe


class HookObserveTests(unittest.TestCase):
    def test_main_normalizes_once_sets_transcript_and_evaluates_prompt(self):
        async def fake_evaluate(prompt: str):
            self.assertEqual(prompt, "Build it")
            self.assertEqual(os.environ["ARTERIES_TRANSCRIPT"], "/tmp/transcript.jsonl")
            self.assertEqual(os.environ["ARTERIES_EVENT"], "prompt")
            self.assertEqual(os.environ["ARTERIES_AGENT_ID"], "demo-hook")
            return "retrieved prompt"

        payload = '{"event":"UserPromptSubmit","prompt":"Build it","transcript_path":"/tmp/transcript.jsonl"}'
        with patch("sys.stdin.isatty", return_value=False), \
             patch("sys.stdin.read", return_value=payload), \
             patch.object(hook_observe, "evaluate", side_effect=fake_evaluate) as evaluate, \
             patch("builtins.print") as print_:
            result = hook_observe.main(["--cli", "claude", "--project", "demo", "--agent", "demo-hook"])

        self.assertEqual(result, 0)
        evaluate.assert_called_once_with("Build it")
        print_.assert_called_once_with("retrieved prompt")


if __name__ == "__main__":
    unittest.main()
