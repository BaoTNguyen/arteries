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

from dbprobe import DB_REACHABLE
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

    @unittest.skipUnless(DB_REACHABLE, "no reachable Postgres; drain writes what it merges")
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

            with patch.object(journal, "_store", side_effect=len):
                counts = journal.drain(tmp)

            # both lines are merged into the record; only the whole one is a row
            self.assertEqual(counts["lines"], 2)
            self.assertEqual(counts["stored"], 1)

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


class SandboxEventsReachTheDatabase(unittest.TestCase):
    """The drain read 54 lines from a sandboxed run and stored 0 of them, then
    deleted the inbox. The filter wanted `source == "arteries"` and an `id`;
    everything heart writes is `source: "heart"` with no id. The journal was the
    channel chosen over mounting the Postgres socket into the container, and it
    was dropping everything that came through it."""

    def test_a_heart_event_is_storable(self):
        event = {"ts": "2026-09-14T12:00:00+00:00", "source": "heart",
                 "kind": "episode.finished", "episode_id": "ep-1"}
        row = journal._storable(event)
        self.assertIsNotNone(row)
        self.assertTrue(row["id"], "an id is synthesised, not required")

    def test_the_synthesised_id_is_stable_so_a_redrain_dedupes(self):
        event = {"ts": "t", "source": "heart", "kind": "role.started"}
        self.assertEqual(journal._storable(dict(event))["id"],
                         journal._storable(dict(event))["id"])
        other = journal._storable({**event, "kind": "role.finished"})
        self.assertNotEqual(journal._storable(dict(event))["id"], other["id"])

    def test_an_arteries_event_keeps_the_id_it_was_given(self):
        given = str(uuid.uuid4())
        self.assertEqual(
            journal._storable({"id": given, "source": "arteries", "kind": "x"})["id"],
            given)

    def test_a_line_that_is_not_an_event_is_still_refused(self):
        self.assertIsNone(journal._storable("not a dict"))
        self.assertIsNone(journal._storable({"source": "heart"}))  # no kind

    def test_the_episode_id_survives_into_the_payload(self):
        """agent_events has no episode_id column, so on the outside of the
        payload it was simply lost -- and with it every query that asks what a
        sandboxed episode did."""
        payload = journal._payload({
            "kind": "role.finished", "episode_id": "ep-1", "task_id": "t-1",
            "role": "implement", "payload": {"passed": True}})
        self.assertEqual(payload["episode_id"], "ep-1")
        self.assertEqual(payload["role"], "implement")
        self.assertTrue(payload["passed"])

    def test_an_explicit_payload_field_wins_over_the_envelope(self):
        payload = journal._payload({"kind": "k", "role": "implement",
                                    "payload": {"role": "test"}})
        self.assertEqual(payload["role"], "test")


class OneEventStreamNotTwo(unittest.TestCase):
    """An inbox is where a writer puts a line when it cannot reach the database.
    Everything else writes to the daily file. The drain read only the first, so
    no event heart ever emitted reached the database -- from a sandbox or from a
    plain local run. Same events, same shape, one reader."""

    def test_the_daily_file_is_ingested_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "20260915.ndjson").write_text(
                '{"ts":"t","source":"heart","kind":"episode.finished","episode_id":"ep-1"}\n')
            with patch.object(journal, "_store", side_effect=len) as store:
                counts = journal.drain(tmp)
            self.assertEqual(counts["stored"], 1)
            self.assertEqual(store.call_args[0][0][0]["kind"], "episode.finished")

    def test_draining_twice_stores_the_same_rows_again_and_the_ids_collide(self):
        """Re-reading the whole file every time is only safe because the id is
        derived from the line: the insert conflicts with itself."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "20260915.ndjson").write_text(
                '{"ts":"t","source":"heart","kind":"role.started"}\n')
            seen = []
            with patch.object(journal, "_store", side_effect=lambda r: seen.append(r) or len(r)):
                journal.drain(tmp)
                journal.drain(tmp)
            self.assertEqual(seen[0][0]["id"], seen[1][0]["id"])

    def test_the_daily_file_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp) / "20260915.ndjson"
            daily.write_text('{"ts":"t","source":"heart","kind":"k"}\n')
            with patch.object(journal, "_store", side_effect=len):
                journal.drain(tmp)
            self.assertTrue(daily.exists(), "the daily file is the record, not a queue")


class AMissingRunDoesNotTakeTheBatchDown(unittest.TestCase):
    """agent_events.run_id is a foreign key and execute_batch sends the batch as
    one statement, so a single event naming a pruned run failed the whole drain
    -- swallowed by the except and reported as `stored: 0`. 30,707 lines sat
    unread behind one bad id."""

    def test_an_unknown_run_is_detached_not_dropped(self):
        events = [{"id": "a", "kind": "k", "run_id": "gone"},
                  {"id": "b", "kind": "k", "run_id": "here"},
                  {"id": "c", "kind": "k"}]
        with patch.object(journal, "_known_run_ids", return_value={"here"}):
            out = journal._detach_unknown_runs(events)
        self.assertIsNone(out[0]["run_id"], "the event survives without the join")
        self.assertEqual(out[1]["run_id"], "here")
        self.assertEqual(len(out), 3)

    def test_nothing_to_check_is_not_a_database_round_trip(self):
        events = [{"id": "a", "kind": "k"}]
        with patch.object(journal, "_known_run_ids") as known:
            self.assertEqual(journal._detach_unknown_runs(events), events)
        known.assert_not_called()
