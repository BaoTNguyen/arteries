import contextlib
import io
import unittest
from unittest.mock import patch

from arteries import cli


class ArtCliTests(unittest.TestCase):
    def test_setup_dispatches_to_setup_cli(self):
        with patch.object(cli.setup_cli, "main", return_value=0) as setup_main:
            result = cli.main(["setup", "--list"])

        self.assertEqual(result, 0)
        setup_main.assert_called_once_with(["--list"])

    def test_docs_dispatches_to_docs_cli(self):
        with patch.object(cli.docs, "main", return_value=0) as docs_main:
            result = cli.main(["docs", "extract", "--project", "."])

        self.assertEqual(result, 0)
        docs_main.assert_called_once_with(["extract", "--project", "."])

    def test_eval_prints_retrieved_prompt(self):
        async def fake_evaluate(prompt: str):
            return f"retrieved: {prompt}"

        with patch.object(cli, "evaluate", side_effect=fake_evaluate) as evaluate, \
             patch("builtins.print") as print_:
            result = cli.main(["eval", "hello", "world"])

        self.assertEqual(result, 0)
        evaluate.assert_called_once_with("hello world")
        print_.assert_called_once_with("retrieved: hello world")

    def test_doctor_dispatches_to_doctor_cli(self):
        with patch.object(cli.doctor, "main", return_value=0) as doctor_main:
            result = cli.main(["doctor", "--project", "demo"])

        self.assertEqual(result, 0)
        doctor_main.assert_called_once_with(["--project", "demo"])

    def test_packet_dispatches_to_packet_cli(self):
        with patch.object(cli.packet, "main", return_value=0) as packet_main:
            result = cli.main(["packet", "--message", "manual"])

        self.assertEqual(result, 0)
        packet_main.assert_called_once_with(["--message", "manual"])

    def test_trace_dispatches_to_trace_cli(self):
        with patch.object(cli.trace, "main", return_value=0) as trace_main:
            result = cli.main(["trace", "--repo", "/tmp/demo"])

        self.assertEqual(result, 0)
        trace_main.assert_called_once_with(["--repo", "/tmp/demo"])


if __name__ == "__main__":
    unittest.main()


class ActivateTests(unittest.TestCase):
    """`art activate` is the session-start hook: what it prints is the context a
    host shows. It called storage.get_evergreen long after the rework deleted
    that function, and swallowed the AttributeError, so every session started
    with an empty memory block and no error anywhere."""

    def _run(self, **patches):
        from arteries import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
             patch.object(cli.runs, "main", return_value=0), \
             patch("arteries.scope.current_project", return_value="arteries"), \
             patch("arteries.storage.get_persistent", **patches) as read:
            cli._activate([])
        return out.getvalue(), read

    def test_prints_persistent_memory_for_the_resolved_project(self):
        text, read = self._run(return_value=[{"fact": "Entities are scoped to the group."}])
        self.assertIn("Entities are scoped to the group.", text)
        # resolved by path, not the "default" that config.PROJECT_ID falls back to
        self.assertEqual(read.call_args.args[0], "arteries")
        self.assertNotIn("evergreen", text.lower())

    def test_a_failed_read_is_reported_not_swallowed(self):
        with patch("arteries.degrade.note") as note:
            text, _ = self._run(side_effect=RuntimeError("db down"))
        note.assert_called_once()
        self.assertIn("ARTERIES MEMORY SYSTEM ACTIVE", text)  # session still starts
