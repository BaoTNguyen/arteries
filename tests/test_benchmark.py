"""Retrieval measurement. Ground truth is built out of the store itself."""

import json
import unittest
from unittest.mock import patch

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


class ContextConsistencyTests(unittest.TestCase):
    """The benchmark's retrieval context must name the project it is measuring.

    It did not, once: `context_from_env()` reads ARTERIES_PROJECT, which nobody
    running `art benchmark` by hand has set, so the context resolved to "default"
    while queries ran against the cwd-resolved project. The scope CTE found no
    members, every expansion returned empty, and three consecutive reports said
    expansion recovered nothing. It was never running.
    """

    def test_run_builds_its_context_from_the_measured_project(self):
        import inspect
        src = inspect.getsource(benchmark.run)
        self.assertIn("project_id=project", src)
        # `env_ctx = ...` is fine; what must not happen is using it directly
        self.assertNotIn("\n    ctx = memory_select.context_from_env()", src)

    def test_run_reports_what_expansion_costs(self):
        import inspect
        src = inspect.getsource(benchmark.run)
        for field in ("claims_added", "useful_added", "noise_ratio", "displaced"):
            self.assertIn(field, src)


class OverlapTests(unittest.TestCase):
    """A query that reuses the claim's own words tests string matching, not
    retrieval. capillaries discarded two of three benchmarks over this."""

    def test_a_verbatim_query_scores_one(self):
        fact = "The stale-claim sweep measures claimed_at rather than source_ts."
        self.assertEqual(benchmark.token_overlap(fact, fact), 1.0)

    def test_a_paraphrase_scores_low(self):
        self.assertLess(
            benchmark.token_overlap(
                "why did the same batch get written twice",
                "The stale-claim sweep measures claimed_at rather than source_ts."),
            0.34)

    def test_short_words_are_ignored(self):
        """'the', 'a', 'is' are shared by everything and mean nothing."""
        self.assertEqual(
            benchmark.token_overlap("is the a of", "nothing in common here"), 0.0)

    def test_an_empty_query_does_not_divide_by_zero(self):
        self.assertEqual(benchmark.token_overlap("", "a fact"), 0.0)

    def test_generated_queries_record_their_overlap(self):
        claims = [{"id": "c1", "fact": "Ephemeral rows expire after 48 hours."}]
        payload = {"choices": [{"message": {"content": json.dumps(
            {"questions": {"0": "how long does the working set last"}})}}]}
        with patch("httpx.post", return_value=_Resp(payload)):
            built = benchmark.build_queries(claims)
        self.assertIn("overlap", built[0])
        self.assertLess(built[0]["overlap"], 0.34)


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload
