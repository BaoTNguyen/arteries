"""The journal is the sandbox's only way out.

One shared daily file is fine while every writer is a host process. Once
containers write too, concurrent appends over PIPE_BUF can interleave, and an
assistant_preview runs to 2000 characters before escaping. Per-run inboxes make
that structurally impossible; the drain folds them back in.
"""
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from arteries import journal


class InboxTests(unittest.TestCase):
    def test_each_run_gets_its_own_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = journal.inbox("run-a", tmp)
            b = journal.inbox("run-b", tmp)
        self.assertNotEqual(a, b)
        self.assertEqual(a.parent.name, "incoming")

    def test_appending_honours_the_mounted_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = journal.inbox("run-a", tmp)
            box.mkdir(parents=True)
            with patch.dict(os.environ, {"EVENT_JOURNAL_DIR": str(box)}):
                journal.journal_append("arteries", "turn.observed",
                                       event_id="e1", run_id="run-a", note="hi")
            written = list(box.glob("*.ndjson"))
            self.assertEqual(len(written), 1)
            event = json.loads(written[0].read_text().splitlines()[0])
        self.assertEqual(event["id"], "e1")
        self.assertEqual(event["run_id"], "run-a")
        self.assertEqual(event["payload"]["note"], "hi")


class DrainTests(unittest.TestCase):
    def _inbox_line(self, root, run_id, **over):
        box = journal.inbox(run_id, root)
        box.mkdir(parents=True, exist_ok=True)
        event = {"ts": "2026-08-25T00:00:00+00:00", "source": "heart",
                 "kind": "role.finished", **over}
        (box / "20260825.ndjson").write_text(json.dumps(event) + "\n")
        return event

    def test_drain_merges_and_empties_every_inbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._inbox_line(tmp, "run-a")
            self._inbox_line(tmp, "run-b")

            counts = journal.drain(tmp)

            self.assertEqual(counts["files"], 2)
            self.assertEqual(counts["lines"], 2)
            merged = list(Path(tmp).glob("*.ndjson"))
            self.assertEqual(len(merged), 1)
            self.assertEqual(len(merged[0].read_text().splitlines()), 2)
            self.assertFalse(any((Path(tmp) / "incoming").iterdir()))

    def test_draining_twice_adds_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._inbox_line(tmp, "run-a")
            journal.drain(tmp)
            self.assertEqual(journal.drain(tmp)["lines"], 0)

    def test_a_partial_last_line_is_kept_but_not_stored(self):
        """A container killed mid-write leaves half a line. It still belongs in
        the record; it just has nothing to insert."""
        with tempfile.TemporaryDirectory() as tmp:
            box = journal.inbox("run-a", tmp)
            box.mkdir(parents=True)
            (box / "20260825.ndjson").write_text('{"source":"heart","kind":"ok"}\n{"sour')

            counts = journal.drain(tmp)

            self.assertEqual(counts["lines"], 2)
            self.assertEqual(counts["stored"], 0)

    def test_an_unreachable_database_leaves_the_inbox_intact(self):
        """The inbox is the only durable copy until the rows land. Deleting it on
        a failed store would lose the events the one time it matters."""
        with tempfile.TemporaryDirectory() as tmp:
            self._inbox_line(tmp, "run-a", source="arteries", id=str(uuid.uuid4()))
            with patch.object(journal, "_store", return_value=0):
                counts = journal.drain(tmp)

            self.assertEqual(counts["files"], 0)
            self.assertTrue(list(journal.inbox("run-a", tmp).glob("*.ndjson")))

    def test_no_incoming_directory_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(journal.drain(tmp)["files"], 0)


if __name__ == "__main__":
    unittest.main()
