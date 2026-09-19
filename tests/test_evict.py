"""Read-side decay.

Finding 7: `access_count` was tracked and never acted on, so nothing ever left
persistent. Write-side filtering is never perfect -- 12.5% of live rows were
transient session intent even after the compiler was told not to write them --
so decay is what bounds the error the filter misses.
"""

import unittest

from dbprobe import DB_REACHABLE

import psycopg2
import pytest

from arteries import activity, evict
from arteries.config import DB_CONFIG

PROJECT = "evict-test"


@unittest.skipUnless(DB_REACHABLE, "no reachable Postgres; eviction is measured against the database clock")
class ClockTests(unittest.TestCase):
    """Activity days, not calendar days: time away must not age out a working
    set that was never wrong."""

    def setUp(self):
        self.conn = psycopg2.connect(**DB_CONFIG)
        self._clean()

    def tearDown(self):
        self._clean()
        self.conn.close()

    def _clean(self):
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM arteries.project_activity WHERE project_id = %s",
                        (PROJECT,))
            cur.execute("DELETE FROM arteries.persistent WHERE project_id = %s",
                        (PROJECT,))
        self.conn.commit()

    def _days(self, n):
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO arteries.project_activity "
                "SELECT %s, current_date - g FROM generate_series(0, %s) g "
                "ON CONFLICT DO NOTHING", (PROJECT, n - 1))
        self.conn.commit()

    def _claim(self, fact, kind="fact", access=0, day=1):
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO arteries.persistent "
                "(fact, kind, domains, confidence, project_id, access_count, "
                " last_activity_day) "
                "VALUES (%s, %s, '[]'::jsonb, 0.9, %s, %s, %s) RETURNING id",
                (fact, kind, PROJECT, access, day))
            row_id = cur.fetchone()[0]
        self.conn.commit()
        return row_id

    def test_touching_twice_in_one_day_is_one_day(self):
        activity.touch(PROJECT)
        activity.touch(PROJECT)
        self.assertEqual(activity.current_day(PROJECT), 1)

    def test_a_row_with_no_day_is_not_assumed_stale(self):
        """Rows predating the clock have no last-useful day. Treating that as
        day zero would evict the entire backlog the first time this ran."""
        self._days(40)
        self._claim("predates the clock", day=None)
        self.assertEqual(evict.candidates(self.conn, PROJECT), [])

    def test_an_unread_old_claim_is_a_candidate(self):
        self._days(40)
        self._claim("nobody ever read this", day=2)
        self.assertEqual(len(evict.candidates(self.conn, PROJECT)), 1)

    def test_a_read_claim_survives(self):
        self._days(40)
        self._claim("someone read this", access=3, day=2)
        self.assertEqual(evict.candidates(self.conn, PROJECT), [])

    def test_a_recent_claim_survives(self):
        self._days(40)
        self._claim("written yesterday", day=39)
        self.assertEqual(evict.candidates(self.conn, PROJECT), [])

    def test_preferences_and_constraints_are_never_evicted_by_age(self):
        """Age and disuse are not evidence a preference stopped being true.
        "Prefers tabs over spaces" is read once a month and true every day."""
        self._days(40)
        for kind in ("preference", "constraint"):
            self._claim(f"an old unread {kind}", kind=kind, day=2)
        self.assertEqual(evict.candidates(self.conn, PROJECT), [])

    def test_a_claim_something_else_refines_survives(self):
        """Load-bearing even if unread."""
        from arteries import graph

        self._days(40)
        target = self._claim("unread but referenced", day=2)
        other = self._claim("the one that refines it", access=5, day=39)
        with self.conn.cursor() as cur:
            graph.add_edge(cur, PROJECT, "persistent", str(other), "refines",
                           "persistent", str(target))
        self.conn.commit()
        self.assertEqual(evict.candidates(self.conn, PROJECT), [])

    def test_eviction_tombstones_rather_than_deletes(self):
        """A retired claim answers "what did we used to think", and
        memory_edges still points at it."""
        self._days(40)
        row_id = self._claim("nobody ever read this", day=2)
        evict.run(PROJECT)
        with self.conn.cursor() as cur:
            cur.execute("SELECT valid_until IS NOT NULL FROM arteries.persistent "
                        "WHERE id = %s", (row_id,))
            tombstoned = cur.fetchone()
        self.assertIsNotNone(tombstoned)
        self.assertTrue(tombstoned[0])

    def test_a_dry_run_changes_nothing(self):
        self._days(40)
        self._claim("nobody ever read this", day=2)
        result = evict.run(PROJECT, dry_run=True)
        self.assertEqual(result["evicted"], 0)
        self.assertEqual(result["candidates"], 1)
        self.assertEqual(len(evict.candidates(self.conn, PROJECT)), 1)

    def test_retention_is_measured_in_activity_days(self):
        """The same row is safe at 10 activity days and stale at 40, whatever
        the calendar says."""
        self._days(10)
        self._claim("nobody ever read this", day=2)
        self.assertEqual(evict.candidates(self.conn, PROJECT), [])
        self._days(40)
        self.assertEqual(len(evict.candidates(self.conn, PROJECT)), 1)
