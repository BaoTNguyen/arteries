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
