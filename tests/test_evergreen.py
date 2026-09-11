"""The evergreen tier and what gets into it.

`arteries.evergreen` has never existed. Three scripts called a module deleted
with the tier, `scripts/watch.sh` called a storage function that was never
written, and capillaries' rework doc still says arteries holds durable facts
there. This is the tier those all assumed.
"""

import unittest

from arteries import evergreen


class ScoreTests(unittest.TestCase):
    def test_the_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(evergreen.WEIGHTS.values()), 1.0)

    def test_a_perfect_claim_scores_one(self):
        self.assertEqual(evergreen.score(1, 1, 1, 1), 1.0)

    def test_novelty_alone_cannot_clear_the_bar(self):
        """0.4 is deliberately below the 0.6 threshold: being unlike everything
        already stored is not by itself evidence of anything."""
        self.assertLess(evergreen.score(1, 0, 0, 0), evergreen.EVERGREEN_THRESHOLD)

    def test_terms_are_clamped(self):
        self.assertEqual(evergreen.score(5, 5, 5, 5), 1.0)
        self.assertEqual(evergreen.score(-1, -1, -1, -1), 0.0)

    def test_none_is_treated_as_zero(self):
        self.assertEqual(evergreen.score(None, None, None, None), 0.0)


class EligibilityTests(unittest.TestCase):
    """Two doors, not one threshold."""

    def test_a_decision_promotes_without_breadth(self):
        """A decision is scope-wide by kind -- a choice made in arteries binds
        heart -- so it does not have to name three things to prove it."""
        self.assertTrue(evergreen.eligible(0.8, "decision", reach=0.0))

    def test_a_narrow_fact_does_not_promote(self):
        """A fact about one file with no reuse stays persistent. That is the
        point of having two tiers."""
        self.assertFalse(evergreen.eligible(0.8, "fact", reach=0.0))

    def test_a_broad_fact_promotes(self):
        self.assertTrue(evergreen.eligible(0.8, "fact", reach=evergreen.MIN_REACH))

    def test_the_threshold_still_binds_a_decision(self):
        self.assertFalse(evergreen.eligible(0.3, "decision", reach=1.0))

    def test_constraints_and_preferences_are_scope_wide_too(self):
        for kind in ("constraint", "preference"):
            self.assertTrue(evergreen.eligible(0.8, kind, reach=0.0), kind)


class DatabaseTests(unittest.TestCase):
    """Promotion against a real table: the scoring queries are most of the
    logic and none of them run without one."""

    @classmethod
    def setUpClass(cls):
        import pytest

        pytest.importorskip("psycopg2")

    def setUp(self):
        import psycopg2

        from arteries.config import DB_CONFIG

        self.scope = "evg-test"
        self.conn = psycopg2.connect(**DB_CONFIG)
        self._clean()

    def tearDown(self):
        self._clean()
        self.conn.close()

    def _clean(self):
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM arteries.evergreen WHERE scope_id = %s", (self.scope,))
            cur.execute("DELETE FROM arteries.persistent WHERE project_id = %s", (self.scope,))
        self.conn.commit()

    def _persistent(self, fact, kind="fact", access=0, embedded=True):
        """A fixed vector rather than a real embedding: novelty is measured
        against what evergreen already holds, and a constant makes that
        measurable without a model."""
        from arteries.config import EMBED_DIM

        vector = [0.1] * EMBED_DIM if embedded else None
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO arteries.persistent "
                "(fact, kind, domains, confidence, project_id, access_count, embedding) "
                "VALUES (%s, %s, '[]'::jsonb, 0.9, %s, %s, %s) RETURNING id",
                (fact, kind, self.scope, access, vector))
            row_id = cur.fetchone()[0]
        self.conn.commit()
        return row_id

    def test_novelty_is_one_against_an_empty_tier(self):
        """The first claim about anything is maximally incremental to a graph
        with nothing in it. Correct rather than generous."""
        from arteries.config import EMBED_DIM

        row = {"id": self._persistent("a claim"), "embedding": [0.1] * EMBED_DIM}
        self.assertEqual(evergreen._novelty(self.conn, row, self.scope), 1.0)

    def test_a_row_with_no_embedding_scores_no_novelty(self):
        row = {"id": self._persistent("a claim", embedded=False), "embedding": None}
        self.assertEqual(evergreen._novelty(self.conn, row, self.scope), 0.0)

    def test_reach_counts_distinct_entities(self):
        from arteries import graph

        row_id = self._persistent("names two things")
        with self.conn.cursor() as cur:
            for n in (1, 2):
                graph.add_edge(cur, self.scope, "persistent", str(row_id), "mentions",
                               "entity", f"4444444{n}-4444-4444-4444-444444444444")
        self.conn.commit()
        self.assertAlmostEqual(
            evergreen._reach(self.conn, {"id": row_id}), 2 / 3)

    def test_a_seeded_core_row_never_scored(self):
        """NULL, not zero. Zero reads as "scored badly and got in anyway"."""
        evergreen.seed(self.conn, self.scope, "the project's own specification")
        self.conn.commit()
        with self.conn.cursor() as cur:
            cur.execute("SELECT core, origin, incrementality FROM arteries.evergreen "
                        "WHERE scope_id = %s", (self.scope,))
            core, origin, incrementality = cur.fetchone()
        self.assertTrue(core)
        self.assertEqual(origin, "authored")
        self.assertIsNone(incrementality)

    def test_promotion_is_idempotent(self):
        """Promoting the same persistent row twice is what the parent_ids unique
        index exists to prevent."""
        self._persistent("We chose Postgres over Neo4j for the graph.", "decision", 3)
        first = evergreen.promote_once(self.scope)
        second = evergreen.promote_once(self.scope)
        self.assertEqual(first["promoted"], 1)
        self.assertEqual(second["promoted"], 0)

    def test_candidates_exclude_what_is_already_promoted(self):
        """One query for the real pass and for --dry-run, so a verdict means the
        same thing in both."""
        self._persistent("We chose Postgres over Neo4j for the graph.", "decision", 3)
        evergreen.promote_once(self.scope)
        self.assertEqual(evergreen.candidates(self.conn, self.scope, 10), [])
