"""Contract test for the ledger seam heart depends on: when Postgres is
unreachable, ledger writes degrade to a repo-local JSONL fallback under
.vascular/arteries/decisions/ and the emitted spine event records where the
write actually landed via a `store` field.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path


from arteries import actionlog
from arteries.config import DB_CONFIG


class DegradationDrillTests(unittest.TestCase):
    """Point the DB at a dead port, trigger a ledger write
    through actionlog's public API, and assert the failure is contained --
    no exception escapes, the JSONL fallback file appears, and the spine
    event's `store` field pins which path actually fired."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "repo").mkdir()
        self._env = dict(os.environ)
        os.environ.update({
            "ARTERIES_REPO": str(self.root / "repo"),
            "ARTERIES_PROJECT": "ledger-degradation-test",
            "ARTERIES_EPISODE_ID": "contract-test-degradation-1",
            "ARTERIES_TASK_ID": "contract-test-task",
            "EVENT_JOURNAL_DIR": str(self.root / "journal"),
            # Documented for operators, but DB_CONFIG is a module-level dict
            # already resolved from env at import time (see arteries/config.py)
            # -- setting env here alone would be a no-op for a live process.
            # We set them anyway for honesty/documentation, then force the
            # dict directly the same way arteries' own test_actionlog.py does.
            "DB_HOST": "127.0.0.1",
            "DB_PORT": "1",
        })
        self._db = dict(DB_CONFIG)
        DB_CONFIG.update({"host": "127.0.0.1", "port": 1})

    def tearDown(self):
        DB_CONFIG.clear()
        DB_CONFIG.update(self._db)
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def _journal_events(self) -> list[dict]:
        events = []
        for path in sorted((self.root / "journal").glob("*.ndjson")):
            events += [json.loads(line) for line in path.read_text().splitlines()]
        return events

    def test_dead_db_degrades_to_jsonl_and_journal_records_store(self):
        try:
            rec = actionlog.log_decision(
                "contract.degradation",
                chosen_action="fallback",
                available_actions=["fallback", "raise"],
                observation={"reason": "dead port drill"},
            )
        except Exception as exc:  # pragma: no cover - the point of this test
            self.fail(f"log_decision raised instead of degrading gracefully: {exc!r}")

        self.assertEqual(rec["episode_id"], "contract-test-degradation-1")

        jsonl_files = list((self.root / "repo" / ".vascular" / "arteries" / "decisions").glob("*.jsonl"))
        self.assertEqual(len(jsonl_files), 1, "expected exactly one JSONL fallback file")

        jsonl_records = [json.loads(line) for line in jsonl_files[0].read_text().splitlines()]
        matching = [r for r in jsonl_records if r.get("id") == rec["id"]]
        self.assertEqual(len(matching), 1)

        journaled = self._journal_events()
        gate = [e for e in journaled if e["kind"] == "decision.contract.degradation"]
        self.assertEqual(len(gate), 1)
        # Pin reality: with a dead DB but a writable tempdir, the jsonl write
        # succeeds, so the emitted store flag is "jsonl" (not "lost"). "lost"
        # only fires if the jsonl write itself also raises.
        self.assertEqual(gate[0]["payload"]["store"], "jsonl")


if __name__ == "__main__":
    unittest.main()
