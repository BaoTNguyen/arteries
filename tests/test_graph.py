"""Graph writes: validation, entity canonicalization, and edge shape."""

import unittest
from unittest.mock import MagicMock, patch

from arteries import compile as compiler
from arteries import graph


class ValidateResponseTests(unittest.TestCase):
    """Grammar-constrained JSON is well-formed, not correct. compile.py has
    validated UUIDs in Python since it was written because the model invents
    ids; entities and relations give it more to invent."""

    def test_a_good_response_has_no_problems(self):
        self.assertEqual(compiler.validate_response({
            "new_memories": [{"fact": "f", "kind": "fact",
                              "entities": [{"name": "pgvector", "kind": "dependency"}],
                              "relations": [{"persistent_id": "x", "rel": "refines"}]}],
            "superseded": [],
        }), [])

    def test_missing_fact_is_caught(self):
        problems = compiler.validate_response({"new_memories": [{"domains": []}]})
        self.assertTrue(any("no fact" in p for p in problems))

    def test_invented_relation_is_caught(self):
        problems = compiler.validate_response({
            "new_memories": [{"fact": "f", "relations": [{"persistent_id": "x", "rel": "vibes"}]}]
        })
        self.assertTrue(any("vibes" in p for p in problems))

    def test_invented_kind_is_caught(self):
        problems = compiler.validate_response({"new_memories": [{"fact": "f", "kind": "musing"}]})
        self.assertTrue(any("musing" in p for p in problems))

    def test_non_object_is_caught_without_raising(self):
        self.assertEqual(compiler.validate_response("nope"), ["response is not an object"])


class EntityTests(unittest.TestCase):
    def test_unmatched_name_is_kept_and_flagged(self):
        """A vocabulary you are still growing must not eat the facts it does
        not cover yet."""
        cur = MagicMock()
        cur.fetchone.return_value = ("id-1", "dspy", "dependency", None, False)
        with patch("arteries.ontology.resolve") as resolve:
            resolve.return_value = type("M", (), {"valid": False, "name": "dspy",
                                                  "uri": None, "score": 0.0})()
            node = graph.upsert_entity(cur, "harness", "dspy", "dependency")
        self.assertFalse(node.ontology_valid)
        self.assertIsNone(node.ontology_class)

    def test_unknown_entity_kind_falls_back_to_concept(self):
        cur = MagicMock()
        cur.fetchone.return_value = ("id-2", "thing", "concept", None, False)
        with patch("arteries.ontology.resolve") as resolve:
            resolve.return_value = type("M", (), {"valid": False, "name": "thing",
                                                  "uri": None, "score": 0.0})()
            graph.upsert_entity(cur, "harness", "thing", "wildlife")
        self.assertEqual(cur.execute.call_args[0][1][3], "concept")

    def test_blank_name_writes_nothing(self):
        cur = MagicMock()
        self.assertIsNone(graph.upsert_entity(cur, "harness", "   "))
        cur.execute.assert_not_called()


class EdgeTests(unittest.TestCase):
    def test_edge_carries_its_metadata(self):
        cur = MagicMock()
        graph.add_edge(cur, "arteries", "persistent", "a", graph.SUPERSEDES,
                       "persistent", "b", metadata={"reason": "replaced by X"})
        params = cur.execute.call_args[0][1]
        self.assertEqual(params[5], "supersedes")
        self.assertIn("reason", params[7].adapted)

    def test_expand_with_no_seeds_does_not_query(self):
        conn = MagicMock()
        self.assertEqual(graph.expand(conn, "arteries", []), [])
        conn.cursor.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class ExpandShapeTests(unittest.TestCase):
    """Expansion must traverse shared entities, not only claim-to-claim edges.

    Most edges leaving a claim go to its provenance or its entities. In a real
    store: 206 derived_from, 52 mentions, and only 33 claim-to-claim. An earlier
    version walked direct edges only and returned nothing, while 210 pairs of
    claims sat one shared entity apart.
    """

    def test_query_covers_both_traversals(self):
        import inspect
        sql = inspect.getsource(graph.expand)
        self.assertIn("shared_entity", sql)
        self.assertIn("direct", sql)
        self.assertIn("'mentions'", sql)

    def test_scope_filter_is_on_the_claim_side(self):
        """A shared entity must not leak one group's claims into another's."""
        import inspect
        sql = inspect.getsource(graph.expand)
        self.assertIn("p.project_id IN (SELECT project_id FROM scope)", sql)

    def test_seeds_are_excluded_from_their_own_expansion(self):
        import inspect
        self.assertIn("NOT (p.id::text = ANY", inspect.getsource(graph.expand))


class SelectionTests(unittest.TestCase):
    def test_thinness_is_measured_by_quality_not_row_count(self):
        """RELEVANCE_THRESHOLD is 0.0 pending calibration, so the query always
        returns its full limit. A count-based gate never fires -- which is what
        happened the first time this was wired."""
        from arteries import memory_select
        self.assertGreater(memory_select.STRONG_SIMILARITY, 0.5)
        self.assertGreater(memory_select.EXPAND_WHEN_STRONG_FEWER_THAN, 0)
        self.assertFalse(hasattr(memory_select, "EXPAND_WHEN_FEWER_THAN"))

    def test_expansion_failure_is_survivable(self):
        from unittest.mock import patch

        from arteries import memory_select
        ctx = memory_select.AgentContext(
            cli="generic", project_id="p", agent_id="a", parent_agent_id=None,
            agent_role="parent", event="prompt",
            capabilities=memory_select.get_capabilities("generic"),
        )
        with patch("psycopg2.connect", side_effect=OSError("down")):
            self.assertEqual(memory_select._expand([{"id": "x"}], ctx, 5), [])
