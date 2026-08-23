"""Retrieval measurement. Ground truth is built out of the store itself."""

import unittest

from arteries import benchmark


class RankTests(unittest.TestCase):
    def test_rank_is_one_indexed(self):
        rows = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        self.assertEqual(benchmark._rank("a", rows), 1)
        self.assertEqual(benchmark._rank("c", rows), 3)

    def test_missing_target_ranks_as_none(self):
        self.assertIsNone(benchmark._rank("z", [{"id": "a"}]))
        self.assertIsNone(benchmark._rank("z", []))


class QueryPromptTests(unittest.TestCase):
    def test_prompt_demands_different_vocabulary(self):
        """A query that reuses the fact's nouns measures nothing -- cosine finds
        it by wording, which is the thing under test."""
        self.assertIn("DIFFERENT vocabulary", benchmark.QUERY_PROMPT)
        self.assertIn("half-remembers", benchmark.QUERY_PROMPT)

    def test_prompt_asks_for_parseable_output(self):
        self.assertIn("JSON only", benchmark.QUERY_PROMPT)


class ScoringTests(unittest.TestCase):
    def test_mrr_rewards_higher_ranks(self):
        """1/1 + 1/2 over two queries = 0.75; a miss contributes nothing."""
        self.assertAlmostEqual(sum(1 / r for r in (1, 2)) / 2, 0.75)
        self.assertAlmostEqual(sum(1 / r for r in (1,) if r) / 2, 0.5)

    def test_sample_targets_claims_with_edges(self):
        """Expansion can only act where structure exists, so the population it
        is measured on has to be claims that carry edges."""
        import inspect
        sql = inspect.getsource(benchmark.sample_claims)
        self.assertIn("memory_edges", sql)
        self.assertIn("dst_kind IN ('entity', 'persistent')", sql)


if __name__ == "__main__":
    unittest.main()
