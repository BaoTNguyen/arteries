"""Telling an outage apart from a defect, inside the same handler."""

import json
import socket
import unittest
from unittest.mock import patch

from arteries import degrade


class ClassificationTests(unittest.TestCase):
    def test_the_world_failing_is_an_environment_error(self):
        for exc in (OSError("refused"), TimeoutError(), socket.timeout(),
                    json.JSONDecodeError("bad", "doc", 0)):
            self.assertTrue(degrade.is_environment(exc), type(exc).__name__)

    def test_a_missing_optional_extra_is_an_environment_error(self):
        """rdflib absent is a deployment fact, not a bug."""
        self.assertTrue(degrade.is_environment(ImportError("no rdflib")))
        self.assertTrue(degrade.is_environment(ModuleNotFoundError("no rdflib")))

    def test_driver_errors_are_matched_by_name(self):
        """psycopg2 and httpx are not imported here; matching is on class name
        so this module stays cheap to import from anywhere."""
        class OperationalError(Exception):
            pass
        class ReadTimeout(Exception):
            pass
        self.assertTrue(degrade.is_environment(OperationalError()))
        self.assertTrue(degrade.is_environment(ReadTimeout()))

    def test_programming_errors_are_not_environment_errors(self):
        for exc in (NameError("storage"), AttributeError("scope"),
                    TypeError("bad"), KeyError("missing")):
            self.assertFalse(degrade.is_environment(exc), type(exc).__name__)


class NoteTests(unittest.TestCase):
    def test_an_outage_reads_as_unavailable_and_stays_quiet(self):
        with patch.object(degrade, "logger") as log:
            reason = degrade.note(OSError("refused"), "lookup")
        self.assertIn("unavailable", reason)
        log.error.assert_not_called()
        log.debug.assert_called_once()

    def test_a_defect_is_loud_and_does_not_claim_unavailability(self):
        """The old handler told users 'Memory storage was unavailable' for any
        failure at all -- a claim about the database it had never checked."""
        # runlog.log_event is patched because it is not a mock here: it writes
        # to the live event store. Unpatched, every run of this suite filed a
        # fabricated internal.bug_swallowed row -- 30 of them by the time anyone
        # looked -- burying the real defects that event exists to surface.
        with patch.object(degrade, "logger") as log, \
             patch("arteries.runlog.log_event") as log_event:
            reason = degrade.note(NameError("storage"), "lookup")
        self.assertNotIn("unavailable", reason)
        self.assertIn("NameError", reason)
        log.error.assert_called_once()
        self.assertTrue(log.error.call_args.kwargs.get("exc_info"))

        # and the event a watcher would act on carries what it needs
        log_event.assert_called_once()
        payload = log_event.call_args.args[2]
        self.assertEqual(payload["where"], "lookup")
        self.assertEqual(payload["error_type"], "NameError")

    def test_reporting_a_bug_never_raises(self):
        """A failure in the reporting path must not become the failure."""
        with patch("arteries.runlog.log_event", side_effect=OSError("db down")):
            self.assertIn("NameError", degrade.note(NameError("x"), "lookup"))


if __name__ == "__main__":
    unittest.main()
