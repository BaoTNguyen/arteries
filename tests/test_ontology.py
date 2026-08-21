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


@unittest.skipUnless(LIVE_TESTS, "set ARTERIES_LIVE_TESTS=1 to run the live loader test")
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
