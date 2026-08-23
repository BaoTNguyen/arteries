import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arteries import doctor


class DoctorTests(unittest.TestCase):
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
