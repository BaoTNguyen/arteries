import tempfile
import pytest
import unittest
from pathlib import Path
from unittest.mock import patch

from arteries import doctor


class DoctorTests(unittest.TestCase):
    @pytest.mark.writes_events
    def test_check_works_with_jsonl_fallback_when_db_is_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(doctor.psycopg2, "connect", side_effect=RuntimeError("db down")):
                report = doctor.check("test-project", "agent-1", "codex", Path(tmp))

        self.assertTrue(report["fallback_ok"])
        self.assertTrue(report["write_ok"])
        self.assertTrue(report["read_ok"])
        self.assertFalse(report["db_ok"])
        self.assertTrue(report["ok"])


if __name__ == "__main__":
    unittest.main()


class EphemeralCollectionTests(unittest.TestCase):
    """Ephemeral is a working set, and nothing has ever collected it.

    A row that was compiled and produced nothing is spent. A row that *did*
    produce a memory is kept regardless of age -- a derived_from edge points at
    it, and provenance back to the raw turn is why those edges exist.
    """

    def test_retention_default_is_conservative(self):
        self.assertGreaterEqual(doctor.EPHEMERAL_RETENTION_DAYS, 7)

    def test_collect_sql_excludes_cited_rows(self):
        sql = doctor._COLLECTABLE_SQL
        self.assertIn("NOT EXISTS", sql)
        self.assertIn("dst_kind = 'ephemeral'", sql)
        self.assertIn("status = 'cleared'", sql)

    def test_collect_never_touches_uncompiled_rows(self):
        self.assertNotIn("uncompiled", doctor._COLLECTABLE_SQL)


class UnreachedTests(unittest.TestCase):
    """Detects the failure this rework produced six times: something written,
    plausible, passing tests, and never invoked."""

    def test_nothing_is_currently_unreached(self):
        self.assertEqual(doctor.unreached(), [])

    def test_a_function_no_one_calls_is_reported(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.py").write_text("def used():\n    pass\n\ndef orphan():\n    pass\n")
            (root / "b.py").write_text("from a import used\n\ndef go():\n    return used()\n")
            found = doctor.unreached(root)
        self.assertIn("a.orphan", found)
        self.assertNotIn("a.used", found)

    def test_a_function_its_own_main_calls_is_reached(self):
        """A CLI entry point invoking its module's helpers counts."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "c.py").write_text("def helper():\n    pass\n\ndef main():\n    return helper()\n")
            self.assertEqual(doctor.unreached(root), [])

    def test_private_functions_are_not_reported(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "d.py").write_text("def _private():\n    pass\n")
            self.assertEqual(doctor.unreached(root), [])


class StoreCleanupTests(unittest.TestCase):
    """Pruning the memory store, not the code.

    Every rule keeps anything still cited. A tombstoned claim that something
    `supersedes` is the answer to "what did we used to think"; deleting it to
    save a row destroys the audit trail the tombstone existed for.
    """

    def test_claim_collection_spares_cited_rows(self):
        sql = doctor._COLLECTABLE_CLAIMS
        self.assertIn("valid_until IS NOT NULL", sql)   # tombstoned only
        self.assertIn("NOT EXISTS", sql)                 # and uncited
        self.assertIn("memory_edges", sql)

    def test_claims_outlive_ephemeral(self):
        """A retired claim answers a question a working set never had."""
        self.assertGreater(doctor.CLAIM_RETENTION_DAYS, doctor.EPHEMERAL_RETENTION_DAYS)

    def test_orphan_entities_are_those_no_live_claim_mentions(self):
        sql = doctor._ORPHAN_ENTITIES
        self.assertIn("dst_kind = 'entity'", sql)
        self.assertIn("p.valid_until IS NULL", sql)

    def test_collectors_are_scoped_to_one_project(self):
        """`art doctor --fix` ran one repo's retention env vars against every
        project in the database: five repos share this scope, and the three
        DELETEs carried no project predicate at all."""
        self.assertIn("e.project_id = %s", doctor._COLLECTABLE_SQL)
        self.assertIn("p.project_id = %s", doctor._COLLECTABLE_CLAIMS)
        # entities are keyed by scope, not project
        self.assertIn("e.scope_id = %s", doctor._ORPHAN_ENTITIES)

    def test_dangling_check_covers_both_ends(self):
        """Checking dst only reported 0 while 49 edges hung off deleted claims;
        chose/over edges have a literal dst, so src is the only catchable end."""
        import inspect
        src = inspect.getsource(doctor.integrity)
        self.assertIn("e.src_kind = 'persistent'", src)

    def test_dangling_sweep_runs_after_deletions(self):
        """Deleting an entity orphans the mentions edges that pointed at it.
        Sweeping first left eight fresh dangling edges behind."""
        import inspect
        src = inspect.getsource(doctor.fix)
        collect_at = src.index("_collect_entities(conn,")
        sweep_at = src.rindex("_retire_dangling(conn)")
        self.assertGreater(sweep_at, collect_at)
