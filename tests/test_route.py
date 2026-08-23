"""Retrieval routing: which strategy a query gets, and on what evidence."""

import unittest

from arteries import route


class NamedCandidateTests(unittest.TestCase):
    """Identifier shapes that matter in this domain."""

    def test_recognises_the_shapes_this_codebase_uses(self):
        cases = {
            "how does MAX_DOC_CHARS work": "max_doc_chars",
            "what does reranker.py do": "reranker.py",
            "the scope_members table": "scope_members",
            "tell me about `find.py`": "find.py",
        }
        for query, expected in cases.items():
            self.assertIn(expected, route.named_candidates(query), query)

    def test_prose_names_nothing(self):
        self.assertEqual(route.named_candidates("why did the cap change"), [])
        self.assertEqual(route.named_candidates("plain english question"), [])

    def test_candidates_are_deduplicated(self):
        self.assertEqual(route.named_candidates("find.py and find.py again"), ["find.py"])


class ChoiceTests(unittest.TestCase):
    def test_strong_seeds_short_circuit_everything(self):
        """Adding neighbours to an answer that already exists spends budget."""
        plan = route.choose("what does reranker.py do", [{"similarity": 0.9}] * 6)
        self.assertEqual(plan.strategy, "cosine")

    def test_weak_seeds_with_a_named_thing_take_the_entity_route(self):
        plan = route.choose("what does reranker.py do", [{"similarity": 0.3}] * 6)
        self.assertEqual(plan.strategy, "cosine+entity")
        self.assertIn("reranker.py", plan.entities)

    def test_weak_seeds_without_a_named_thing_expand(self):
        plan = route.choose("why did the cap change", [{"similarity": 0.3}] * 6)
        self.assertEqual(plan.strategy, "cosine+expansion")

    def test_no_seeds_at_all_still_routes(self):
        self.assertEqual(route.choose("anything", []).strategy, "cosine+expansion")

    def test_the_reason_is_recorded_for_the_decision_ledger(self):
        plan = route.choose("what does reranker.py do", [{"similarity": 0.3}])
        payload = plan.as_payload()
        self.assertIn("strategy", payload)
        self.assertTrue(payload["reason"])
        self.assertEqual(payload["strong_seeds"], 0)


if __name__ == "__main__":
    unittest.main()
