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
