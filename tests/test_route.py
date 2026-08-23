"""Retrieval routing: which strategy a query gets, and on what evidence."""

import unittest

from arteries import route


class ChoiceTests(unittest.TestCase):
    """Routing reads the seeds, never the query text.

    Cognee classifies the query with regexes before retrieving. That is the
    approach this codebase measured failing -- extract.py shipped four patterns
    and three never matched across 202 rows -- so the decision is made from the
    similarity distribution the embedding already paid for.
    """

    def test_strong_seeds_skip_the_walk(self):
        """Expansion costs about seven extra claims per query. Not worth paying
        when similarity already answered."""
        plan = route.choose([{"similarity": 0.9}] * 6)
        self.assertEqual(plan.strategy, "cosine")
        self.assertEqual(plan.strong_seeds, 6)

    def test_weak_seeds_expand(self):
        plan = route.choose([{"similarity": 0.3}] * 6)
        self.assertEqual(plan.strategy, "cosine+expansion")
        self.assertEqual(plan.strong_seeds, 0)

    def test_a_few_strong_seeds_still_expand(self):
        plan = route.choose([{"similarity": 0.9}] * 2 + [{"similarity": 0.2}] * 4)
        self.assertEqual(plan.strategy, "cosine+expansion")

    def test_no_seeds_at_all_still_routes(self):
        self.assertEqual(route.choose([]).strategy, "cosine+expansion")

    def test_missing_similarity_is_treated_as_zero(self):
        self.assertEqual(route.choose([{}] * 6).strategy, "cosine+expansion")

    def test_the_reason_is_recorded_for_the_decision_ledger(self):
        payload = route.choose([{"similarity": 0.3}]).as_payload()
        self.assertEqual(payload["strategy"], "cosine+expansion")
        self.assertTrue(payload["reason"])
        self.assertNotIn("entities", payload)

    def test_routing_does_not_read_the_query(self):
        """No query parameter means no query-text heuristics can creep back in."""
        import inspect
        self.assertNotIn("query", inspect.signature(route.choose).parameters)


if __name__ == "__main__":
    unittest.main()
