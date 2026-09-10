"""T-Box grounding.

The pure-matching tests run anywhere. The load path needs both Postgres and
rdflib, so it sits behind ARTERIES_LIVE_TESTS like the other live tests.
"""

import os
import unittest
from unittest.mock import patch

from arteries import ontology

LIVE_TESTS = os.getenv("ARTERIES_LIVE_TESTS") == "1"

# A stand-in T-Box shaped like what load() writes: label key, alias keys, kind.
FAKE_TBOX = {
    "entity": ("http://www.w3.org/ns/prov#Entity", "class", "Entity"),
    "was_derived_from": ("http://www.w3.org/ns/prov#wasDerivedFrom", "property", "wasDerivedFrom"),
    "has_broader": ("http://www.w3.org/2004/02/skos/core#broader", "property", "has broader"),
    "broader": ("http://www.w3.org/2004/02/skos/core#broader", "property", "has broader"),
}


class NormalizeTests(unittest.TestCase):
    def test_camel_case_and_prose_reach_the_same_key(self):
        """Ontologies name properties wasDerivedFrom; LLMs write "was derived from"."""
        self.assertEqual(ontology.normalize("wasDerivedFrom"), "was_derived_from")
        self.assertEqual(ontology.normalize("was derived from"), "was_derived_from")
        self.assertEqual(ontology.normalize("  Was-Derived_From!  "), "was_derived_from")


class ResolveTests(unittest.TestCase):
    def setUp(self):
        ontology.reset_cache()
        self._patch = patch.object(ontology, "_lookup", return_value=FAKE_TBOX)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(ontology.reset_cache)

    def test_exact_match_canonicalizes(self):
        match = ontology.resolve("wasDerivedFrom")
        self.assertTrue(match.valid)
        self.assertEqual(match.uri, "http://www.w3.org/ns/prov#wasDerivedFrom")
        self.assertEqual(match.score, 1.0)

    def test_alias_key_matches_when_the_label_would_not(self):
        """SKOS labels skos:broader "has broader", so "broader" only hits via
        the URI-fragment alias -- 0.74 against the label misses at any cutoff."""
        match = ontology.resolve("broader")
        self.assertTrue(match.valid)
        self.assertEqual(match.uri, "http://www.w3.org/2004/02/skos/core#broader")

    def test_kind_filter_picks_the_class_over_the_property(self):
        """PROV-O has both prov:Entity and prov:entity. Asking for a class must
        not hand back the relation."""
        match = ontology.resolve("entity", kind="class")
        self.assertEqual(match.uri, "http://www.w3.org/ns/prov#Entity")
        self.assertEqual(match.kind, "class")

    def test_fuzzy_match_within_cutoff(self):
        match = ontology.resolve("derived from", cutoff=0.7)
        self.assertTrue(match.valid)
        self.assertEqual(match.uri, "http://www.w3.org/ns/prov#wasDerivedFrom")
        self.assertLess(match.score, 1.0)

    def test_unmatched_is_kept_not_dropped(self):
        """An ontology you are still growing must not eat facts it doesn't cover."""
        match = ontology.resolve("banana pancakes")
        self.assertFalse(match.valid)
        self.assertIsNone(match.uri)
        self.assertEqual(match.name, "banana pancakes")

    def test_wrong_match_is_refused_rather_than_forced(self):
        """A bad canonicalization asserts a false identity; a miss just keeps
        the raw name. The cutoff exists to prefer the miss."""
        self.assertFalse(ontology.resolve("postgres").valid)

    def test_empty_tbox_grounds_everything_unmatched(self):
        with patch.object(ontology, "_lookup", return_value={}):
            match = ontology.resolve("wasDerivedFrom")
        self.assertFalse(match.valid)
        self.assertEqual(match.name, "wasDerivedFrom")


def _rdflib_available() -> bool:
    try:
        import rdflib  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(LIVE_TESTS, "set ARTERIES_LIVE_TESTS=1 to run the live loader test")
@unittest.skipUnless(_rdflib_available(),
                     "rdflib is the [ontology] extra; loading needs it, resolving does not")
class LoadTests(unittest.TestCase):
    def test_load_then_resolve_without_rdflib_on_the_read_path(self):
        import pathlib
        fixture = pathlib.Path(__file__).parent / "fixtures" / "mini.ttl"
        result = ontology.load(fixture, source="arteries-test")
        self.assertGreaterEqual(result["terms"], 2)

        ontology.reset_cache()
        self.assertTrue(ontology.resolve("Claim", kind="class").valid)


if __name__ == "__main__":
    unittest.main()


class PredicateBindingTests(unittest.TestCase):
    """Finding 16: `ontology_valid` was false on all 3219 live edges because
    nothing ever set it -- the column read as "no edge is grounded" when it meant
    "nobody checked"."""

    def test_provenance_relations_are_bound(self):
        self.assertEqual(ontology.predicate_uri("derived_from"),
                         "http://www.w3.org/ns/prov#wasDerivedFrom")
        self.assertEqual(ontology.predicate_uri("supersedes"),
                         "http://www.w3.org/ns/prov#wasRevisionOf")

    def test_invented_relations_stay_unbound(self):
        """`supports` and `contradicts` have no faithful standard predicate.
        skos:related asserts far less and would be a false grounding, which is
        worse than a missing one -- the flag exists to tell those apart."""
        self.assertIsNone(ontology.predicate_uri("supports"))
        self.assertIsNone(ontology.predicate_uri("contradicts"))

    def test_lookup_is_case_and_whitespace_insensitive(self):
        self.assertIsNotNone(ontology.predicate_uri("  Derived_From "))

    def test_an_unknown_relation_is_not_an_error(self):
        self.assertIsNone(ontology.predicate_uri("invented_yesterday"))
        self.assertIsNone(ontology.predicate_uri(""))


class ScopedVocabularyTests(unittest.TestCase):
    """One flat `ontology_terms` table meant a vocabulary loaded for one domain
    grounded names in every other -- load finance and "position" inside a coding
    scope resolves to a securities holding."""

    def setUp(self):
        ontology.reset_cache()

    def tearDown(self):
        ontology.reset_cache()

    def test_the_cache_is_keyed_by_scope(self):
        """A single cache would serve whichever scope asked first to both."""
        self.assertIsInstance(ontology._cache, dict)

    def test_resolve_accepts_a_scope(self):
        import inspect

        self.assertIn("scope_id", inspect.signature(ontology.resolve).parameters)

    def test_an_unbound_scope_reads_everything(self):
        """Inert until someone binds a scope, so this changes nothing today."""
        source = inspect_source(ontology._lookup)
        self.assertIn("if sources:", source)
        self.assertIn("FROM arteries.ontology_terms\"", source)


def inspect_source(fn):
    import inspect

    return inspect.getsource(fn)
