"""Injected context must not read as something the user said.

A retrieved prompt arrives in the same channel the user's own words do. Three
were injected into one Claude session at confidence 0.83-0.89, and the assistant
called it a "hook misfire" twice before working out that retrieval had simply
fired -- then acted on corpus text as though it were an instruction.

Three hook paths printed that text. Two labelled it, with different dashes; the
third -- the one Claude Code actually runs -- printed it bare, which is why the
smoke script never showed the problem. The wrapping now happens where the text
is produced, so no wrapper can forget.
"""
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from arteries import eval as arteries_eval
from arteries import hook_observe
from arteries.eval import RETRIEVED_CLOSE, RETRIEVED_OPEN, frame_retrieved


class MarkerTests(unittest.TestCase):
    def test_retrieved_text_is_delimited(self):
        out = frame_retrieved("Run `git diff` and review all changes.")
        self.assertTrue(out.startswith(RETRIEVED_OPEN))
        self.assertTrue(out.endswith(RETRIEVED_CLOSE))
        self.assertIn("Run `git diff`", out)

    def test_the_marker_says_it_is_not_the_user_speaking(self):
        """The specific misreading this exists to prevent."""
        out = frame_retrieved("anything").lower()
        self.assertIn("not an instruction from the user", out)

    def test_every_python_entry_point_wraps(self):
        """Both mains print the result. The marker is applied before either
        wrapper sees the text, so a shell that forgets cannot strip it."""
        async def _retrieved(_message):
            return "corpus text"

        buf = io.StringIO()
        with patch.object(arteries_eval, "evaluate", _retrieved), \
                patch.object(arteries_eval.sys, "argv", ["arteries.eval", "a question"]), \
                redirect_stdout(buf):
            arteries_eval.main()
        self.assertIn(RETRIEVED_OPEN, buf.getvalue(), "arteries.eval")

        buf = io.StringIO()
        with patch.object(hook_observe, "evaluate", _retrieved), \
                patch.object(hook_observe, "read_stdin_json", return_value={}), \
                patch.object(hook_observe, "_message", return_value="a question"), \
                patch.object(hook_observe, "_transcript", return_value=None), \
                patch.object(hook_observe, "normalize"), \
                patch.object(hook_observe, "apply_event_env"), \
                redirect_stdout(buf):
            hook_observe.main([])
        self.assertIn(RETRIEVED_OPEN, buf.getvalue(), "arteries.hook_observe")

    def test_nothing_is_printed_when_retrieval_abstains(self):
        async def _abstained(_message):
            return None

        buf = io.StringIO()
        with patch.object(arteries_eval, "evaluate", _abstained), \
                patch.object(arteries_eval.sys, "argv", ["arteries.eval", "thanks"]), \
                redirect_stdout(buf):
            arteries_eval.main()
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
