"""Every command that a human types must resolve the project from the cwd.

config.PROJECT_ID is os.getenv("ARTERIES_PROJECT", "default"). Hooks export
that variable, so hook paths were always right; a person typing the command
got "default" and silently addressed a project holding nothing. `art search`
returned no rows, `art runs` reported an empty project, and `art docs import`
filed reviewed facts where nothing reads them.
"""
import argparse
import unittest
from unittest.mock import patch

from arteries import runs


class RunsTests(unittest.TestCase):
    def test_project_falls_back_to_the_cwd_not_the_env_default(self):
        ns = argparse.Namespace(project=None)
        with patch("arteries.scope.current_project", return_value="arteries"):
            self.assertEqual(runs._project(ns), "arteries")

    def test_an_explicit_flag_still_wins(self):
        ns = argparse.Namespace(project="capillaries")
        with patch("arteries.scope.current_project", return_value="arteries") as resolve:
            self.assertEqual(runs._project(ns), "capillaries")
        resolve.assert_not_called()

    def test_the_parser_does_not_bind_a_project_at_import(self):
        # default=PROJECT_ID was evaluated when the module loaded, before any
        # cwd was known; the fix is a None default resolved at use.
        with patch("arteries.runlog.recent_events", return_value=[]) as recent, \
             patch("arteries.scope.current_project", return_value="arteries"):
            runs.main(["recent", "--limit", "1"])
        self.assertEqual(recent.call_args.args[0], "arteries")


class SearchTests(unittest.TestCase):
    def test_search_queries_the_resolved_project(self):
        from arteries import cli
        with patch("arteries.scope.current_project", return_value="arteries") as resolve, \
             patch("psycopg2.connect", side_effect=RuntimeError("stop here")):
            with self.assertRaises(RuntimeError):
                cli._search(["MAX_DOC_CHARS"])
        resolve.assert_called_once()


class DocsTests(unittest.TestCase):
    def test_import_writes_to_the_resolved_project(self):
        import inspect

        from arteries import docs
        src = inspect.getsource(docs.import_review)
        self.assertIn("project_id=project", src)
        self.assertNotIn("PROJECT_ID", src)


if __name__ == "__main__":
    unittest.main()
