"""The evidence ladder.

Finding 22: it had one rung. Everything stored was `stated` -- someone said it
in a session. Nothing was `observed`: the test passed, the file existed, the
command exited 0. With one rung, two contradicting claims can only be ordered by
recency, so a confident wrong claim beats a quiet correct one.
"""

import io
import json
import sys
import unittest

from dbprobe import DB_REACHABLE
from unittest.mock import patch

from arteries import evidence


class LadderTests(unittest.TestCase):
    def test_the_order_is_strongest_first(self):
        self.assertEqual(evidence.LADDER, ("user", "observed", "stated", "inferred"))

    def test_ranks_are_the_order(self):
        ranks = [evidence.rank(c) for c in evidence.LADDER]
        self.assertEqual(ranks, sorted(ranks))

    def test_an_unknown_class_sorts_last_rather_than_raising(self):
        """Memory must not fail a turn over a label it does not recognise."""
        self.assertEqual(evidence.rank("nonsense"), len(evidence.LADDER))

    def test_none_is_the_default_class(self):
        self.assertEqual(evidence.rank(None), evidence.rank(evidence.DEFAULT))


class SupersedeTests(unittest.TestCase):
    def test_stronger_evidence_may_retire_weaker(self):
        self.assertTrue(evidence.can_supersede("user", "inferred"))
        self.assertTrue(evidence.can_supersede("observed", "stated"))

    def test_weaker_evidence_may_not(self):
        """An inferred "prefers spaces" must not retire a stated "I prefer
        tabs" -- that also makes the eviction exemption for preferences
        worthless, since the row survives age and is overwritten instead."""
        self.assertFalse(evidence.can_supersede("inferred", "user"))
        self.assertFalse(evidence.can_supersede("stated", "observed"))

    def test_equal_evidence_may(self):
        """Later beats earlier at the same class -- that is what recency is for
        once the ladder has done its work."""
        self.assertTrue(evidence.can_supersede("stated", "stated"))


class SourceMappingTests(unittest.TestCase):
    def test_the_user_is_the_top_rung(self):
        self.assertEqual(evidence.for_source("user"), "user")

    def test_a_tool_result_is_observed(self):
        self.assertEqual(evidence.for_source("tool"), "observed")

    def test_services_report_what_happened(self):
        """heart, plexus and marrow report outcomes rather than opinions, which
        is the same kind of evidence a tool result is."""
        for service in ("heart", "plexus", "marrow"):
            self.assertEqual(evidence.for_source(service), "observed", service)

    def test_an_assistant_reply_is_only_stated(self):
        self.assertEqual(evidence.for_source("assistant"), "stated")

    def test_an_unknown_source_gets_the_default(self):
        self.assertEqual(evidence.for_source("something-new"), evidence.DEFAULT)


class ObservationTests(unittest.TestCase):
    """The hook filters; this writes. Observations are events, not ephemeral
    rows -- evidence the compiler can weigh, not a memory competing for the
    fifteen packet slots."""

    def _run(self, payload):
        from arteries import observe_tool

        written = []
        with patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
             patch("arteries.runlog.log_event",
                   lambda t, s, p=None, **k: written.append((t, p))):
            observe_tool.main()
        return written

    def test_a_failure_is_recorded(self):
        written = self._run({"tool": "Bash", "exit_code": 1, "failed": True,
                             "target": "pytest tests/"})
        self.assertEqual(written[0][0], "tool.result")
        self.assertTrue(written[0][1]["failed"])

    def test_output_is_never_recorded(self):
        """Unbounded, the expensive part, and nothing downstream reads it."""
        written = self._run({"tool": "Bash", "exit_code": 1, "failed": True,
                             "target": "x", "output": "a" * 10_000})
        self.assertNotIn("output", written[0][1])

    def test_the_target_is_bounded(self):
        written = self._run({"tool": "Bash", "failed": True, "target": "x" * 5_000})
        self.assertLessEqual(len(written[0][1]["target"]), 200)

    def test_a_nameless_tool_writes_nothing(self):
        self.assertEqual(self._run({"exit_code": 0}), [])

    def test_malformed_input_is_not_an_error(self):
        from arteries import observe_tool

        with patch.object(sys, "stdin", io.StringIO("{not json")):
            self.assertEqual(observe_tool.main(), 0)


@unittest.skipUnless(DB_REACHABLE, "no reachable Postgres; the write path ends in Postgres")
class WritePathEvidenceTests(unittest.TestCase):
    """`_write_results` must run against a real batch.

    The evidence change shipped a NameError to main: it read a `claimed` local
    that only exists in `compile_once`, so every real pass died with
    "write_error" while the suite stayed green -- the only test touching this
    function patched `_reject_duplicates` to raise first, and nothing ran the
    whole thing. This runs the whole thing.
    """

    @classmethod
    def setUpClass(cls):
        import pytest

        pytest.importorskip("psycopg2")

    def setUp(self):
        import psycopg2

        from arteries.config import DB_CONFIG

        self.project = "evidence-write"
        self.conn = psycopg2.connect(**DB_CONFIG)
        self._clean()

    def tearDown(self):
        self._clean()
        self.conn.close()

    def _clean(self):
        with self.conn.cursor() as cur:
            for table in ("ephemeral", "persistent"):
                cur.execute(f"DELETE FROM arteries.{table} WHERE project_id = %s",
                            (self.project,))
        self.conn.commit()

    def _ephemeral(self, fact, source):
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO arteries.ephemeral "
                "(fact, domains, project_id, agent_process_id, source) "
                "VALUES (%s, '[]'::jsonb, %s, 'test', %s) RETURNING id",
                (fact, self.project, source))
            row_id = cur.fetchone()[0]
        self.conn.commit()
        return str(row_id)

    def _write(self, claimed_ids, fact="a compiled claim about compile.py"):
        from unittest.mock import patch

        from arteries import compile as compile_mod
        from arteries.config import EMBED_DIM

        result = {"new_memories": [{"fact": fact, "kind": "fact",
                                    "confidence": 0.9, "entities": []}]}
        with patch("arteries.embed.embed_texts_sync",
                   lambda texts: [[0.1] * EMBED_DIM for _ in texts]):
            return compile_mod._write_results(self.conn, result, claimed_ids,
                                              self.project)

    def _stored_evidence(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT evidence FROM arteries.persistent "
                        "WHERE project_id = %s", (self.project,))
            return [row[0] for row in cur.fetchall()]

    def test_a_real_batch_writes_without_raising(self):
        written = self._write([self._ephemeral("the user said a thing", "user")])
        self.assertEqual(written["new"], 1)

    def test_a_user_turn_carries_user_evidence(self):
        self._write([self._ephemeral("the user said a thing", "user")])
        self.assertEqual(self._stored_evidence(), ["user"])

    def test_an_assistant_turn_is_only_stated(self):
        self._write([self._ephemeral("the assistant concluded a thing", "assistant")])
        self.assertEqual(self._stored_evidence(), ["stated"])

    def test_a_mixed_batch_takes_the_strongest(self):
        """A batch is one turn's claims and they share a source; when they do
        not, the strongest is the honest label for what produced them."""
        ids = [self._ephemeral("said by the user", "user"),
               self._ephemeral("said by the assistant", "assistant")]
        self._write(ids)
        self.assertEqual(self._stored_evidence(), ["user"])

    def test_an_empty_batch_still_writes(self):
        """`art ingest` calls this with no claimed ids at all."""
        written = self._write([])
        self.assertEqual(written["new"], 1)
        self.assertEqual(self._stored_evidence(), ["stated"])
