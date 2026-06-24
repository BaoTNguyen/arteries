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
