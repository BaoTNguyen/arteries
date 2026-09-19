"""The evergreen tier and what gets into it.

`arteries.evergreen` has never existed. Three scripts called a module deleted
with the tier, `scripts/watch.sh` called a storage function that was never
written, and capillaries' rework doc still says arteries holds durable facts
there. This is the tier those all assumed.
"""

import unittest

from dbprobe import DB_REACHABLE

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


class _Fixture(unittest.TestCase):
    """Shared setup, and no tests of its own.

    MaturityTests subclassed DatabaseTests at first, which silently re-ran every
    inherited test under a second name -- so a failure appeared twice and the
    class that owned it was not obvious.
    """

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
            cur.execute("DELETE FROM arteries.project_activity WHERE project_id = %s",
                        (self.scope,))
        self.conn.commit()

    def _activity_days(self, n):
        """Promotion is consolidation: a claim has to survive some days of real
        work before `durability` and `use` mean anything."""
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO arteries.project_activity "
                "SELECT %s, current_date - g FROM generate_series(0, %s) g "
                "ON CONFLICT DO NOTHING", (self.scope, n - 1))
        self.conn.commit()

    def _aged_claim(self, days_ago: int, fact="We chose Postgres over Neo4j.",
                    kind="decision", access=3):
        """A claim written `days_ago` days back, with activity recorded on every
        day since. Maturity is activity days *since the claim was written*, so a
        claim written today is one day old however long the project has run."""
        from arteries.config import EMBED_DIM

        self._activity_days(days_ago + 1)
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO arteries.persistent "
                "(fact, kind, domains, confidence, project_id, access_count, "
                " valid_from, embedding) "
                "VALUES (%s, %s, '[]'::jsonb, 0.9, %s, %s, "
                "        now() - (%s || ' days')::interval, %s) RETURNING id",
                (fact, kind, self.scope, access, days_ago, [0.1] * EMBED_DIM))
            row_id = cur.fetchone()[0]
        self.conn.commit()
        return row_id

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


@unittest.skipUnless(DB_REACHABLE, "no reachable Postgres; the evergreen tier is a database contract")
class DatabaseTests(_Fixture):
    """Promotion against a real table: the scoring queries are most of the logic
    and none of them run without one."""

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
        self._aged_claim(evergreen.MIN_MATURITY_DAYS + 1)
        first = evergreen.promote_once(self.scope)
        second = evergreen.promote_once(self.scope)
        self.assertEqual(first["promoted"], 1)
        self.assertEqual(second["promoted"], 0)

    def test_candidates_exclude_what_is_already_promoted(self):
        """One query for the real pass and for --dry-run, so a verdict means the
        same thing in both."""
        self._aged_claim(evergreen.MIN_MATURITY_DAYS + 1)
        evergreen.promote_once(self.scope)
        self.assertEqual(evergreen.candidates(self.conn, self.scope, 10), [])


@unittest.skipUnless(DB_REACHABLE, "no reachable Postgres; maturity is scored in SQL")
class MaturityTests(_Fixture):
    """Promotion is consolidation, not a fourth step of ingestion.

    Two of the four terms are meaningless on a fresh claim. `durability` is "not
    superseded within 14 days", which a claim written this turn satisfies by not
    having existed long enough to be contradicted. `use` is access_count/3, and
    a claim written this turn has been read zero times. A brand-new claim can
    clear 0.6 on novelty and reach alone, with no evidence for half its score.
    """

    def test_a_fresh_claim_is_not_a_candidate(self):
        self._activity_days(1)
        self._persistent("We chose Postgres over Neo4j for the graph.", "decision", 3)
        self.assertEqual(evergreen.candidates(self.conn, self.scope, 10), [])

    def test_a_claim_that_survived_enough_days_is(self):
        self._aged_claim(evergreen.MIN_MATURITY_DAYS + 1)
        self.assertEqual(len(evergreen.candidates(self.conn, self.scope, 10)), 1)

    def test_days_are_counted_as_activity_not_calendar(self):
        """Three weeks away should not mature a claim into the graph any more
        than it should age one out.

        The clock has to start *before* the claim, or the pre-clock rule makes
        it mature for a different and also correct reason.
        """
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO arteries.project_activity VALUES "
                        "(%s, current_date - 90) ON CONFLICT DO NOTHING", (self.scope,))
            cur.execute(
                "INSERT INTO arteries.persistent "
                "(fact, kind, domains, confidence, project_id, access_count, valid_from) "
                "VALUES ('an old claim nobody worked on', 'decision', '[]'::jsonb, 0.9, "
                "%s, 3, now() - interval '60 days')", (self.scope,))
        self.conn.commit()
        self._activity_days(2)      # two days of work, 60 calendar days later
        self.assertEqual(evergreen.candidates(self.conn, self.scope, 10), [])

    def test_a_claim_predating_the_clock_is_mature(self):
        """The activity clock started after the store did. Months of real
        survival should not be discounted because nothing was recording days --
        that would measure the clock's age rather than the claim's."""
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO arteries.persistent "
                "(fact, kind, domains, confidence, project_id, access_count, valid_from) "
                "VALUES ('a claim from before the clock', 'decision', '[]'::jsonb, 0.9, "
                "%s, 3, now() - interval '60 days')", (self.scope,))
        self.conn.commit()
        self._activity_days(2)          # clock starts today, claim is older
        self.assertEqual(len(evergreen.candidates(self.conn, self.scope, 10)), 1)

    def test_maturity_counts_from_the_claim_not_the_project(self):
        """A project with a long history does not mature a claim written today.
        The count is activity days *since* the claim, not activity days total."""
        self._activity_days(evergreen.MIN_MATURITY_DAYS * 3)
        self._persistent("written today, in an old project", "decision", 3)
        self.assertEqual(evergreen.candidates(self.conn, self.scope, 10), [])

    def test_what_the_write_filter_rejects_is_not_promoted(self):
        """566 live claims were written before `worth_keeping` existed, and the
        first dry run wanted to promote "User intends to write Plexus-related
        context into a markdown file" into the graph."""
        self._aged_claim(evergreen.MIN_MATURITY_DAYS + 1,
                         fact="User intends to write the Plexus context file.")
        self.assertEqual(evergreen.candidates(self.conn, self.scope, 10), [])
