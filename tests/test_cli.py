import unittest
from unittest.mock import patch

from arteries import cli


class ArtCliTests(unittest.TestCase):
    def test_setup_dispatches_to_setup_cli(self):
        with patch.object(cli.setup_cli, "main", return_value=0) as setup_main:
            result = cli.main(["setup", "--list"])

        self.assertEqual(result, 0)
        setup_main.assert_called_once_with(["--list"])

    def test_evergreen_dispatches_to_evergreen_cli(self):
        with patch.object(cli.evergreen, "main", return_value=0) as evergreen_main:
            result = cli.main(["evergreen", "extract", "--project", "."])

        self.assertEqual(result, 0)
        evergreen_main.assert_called_once_with(["extract", "--project", "."])

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


if __name__ == "__main__":
    unittest.main()
