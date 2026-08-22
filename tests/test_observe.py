"""The write path for services that are not a person at a CLI."""

import unittest
from unittest.mock import patch

from arteries import observe


class ObserveTests(unittest.TestCase):
    def setUp(self):
        self.insert = patch.object(observe.storage, "insert_ephemeral",
                                   return_value="row-1").start()
        self.log = patch.object(observe.runlog, "log_event").start()
        self.addCleanup(patch.stopall)

    def _tracked(self, yes=True):
        return patch.object(observe.scope, "scope_for", return_value="harness" if yes else None)

    def test_source_gets_a_stable_bucket_not_a_pid(self):
        """`art observe` is one-shot. Filing under the caller's pid would strand
        every observation in a bucket the compile claim never revisits."""
        with self._tracked():
            observe.observe("heart chose sequential retries over a pool", 
                            source="heart", project_id="arteries")
        self.assertEqual(self.insert.call_args.kwargs["agent_process_id"], "heart-observer")

    def test_kind_is_tagged_into_the_fact(self):
        with self._tracked():
            observe.observe("decomposed the goal into seven phases",
                            source="plexus", kind="plan", project_id="arteries")
        self.assertTrue(self.insert.call_args.kwargs["fact"].startswith("[plan] "))

    def test_plain_observation_is_not_tagged(self):
        with self._tracked():
            observe.observe("the retry change cut latency substantially",
                            source="heart", project_id="arteries")
        self.assertFalse(self.insert.call_args.kwargs["fact"].startswith("["))

    def test_untracked_project_is_refused(self):
        """Opt-in applies to programmatic senders too, and an explicit project
        is judged on its own registration -- not on the caller's directory."""
        with self._tracked(False), patch.object(observe.scope, "is_tracked", return_value=True):
            row = observe.observe("this names a project nobody registered",
                                  source="heart", project_id="not-registered")
        self.assertIsNone(row)
        self.insert.assert_not_called()

    def test_unknown_source_falls_back_rather_than_raising(self):
        with self._tracked():
            observe.observe("something a stranger sent us about the system",
                            source="wildlife", project_id="arteries")
        self.assertEqual(self.insert.call_args.kwargs["source"], "external")

    def test_too_short_is_dropped(self):
        with self._tracked():
            self.assertIsNone(observe.observe("done", source="heart", project_id="arteries"))
        self.insert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
