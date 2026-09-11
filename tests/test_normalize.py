"""One sentence, once.

Splitting is deliberately conservative. An over-split fragment fails the word
gate and disappears; an under-split pair is one atom that dedupes slightly
worse. The second is cheaper, so ambiguity joins.
"""

import unittest

from arteries.normalize import (MAX_ATOM_CHARS, MIN_ATOM_WORDS, atoms,
                                fact_hash, normalize_fact)


class SplittingTests(unittest.TestCase):
    def test_two_full_sentences_split(self):
        self.assertEqual(
            atoms("The sweep measures claimed_at now. "
                  "It used to measure source_ts, which was the bug."),
            ["The sweep measures claimed_at now.",
             "It used to measure source_ts, which was the bug."])

    def test_an_abbreviation_does_not_end_a_sentence(self):
        self.assertEqual(
            atoms("We use e.g. pgvector for retrieval. "
                  "That is the only vector store in the stack."),
            ["We use e.g. pgvector for retrieval.",
             "That is the only vector store in the stack."])

    def test_a_version_number_does_not_end_a_sentence(self):
        self.assertEqual(len(atoms(
            "Version v1.2 released today with the compile filter on by default.")), 1)

    def test_bullets_are_their_own_atoms_without_punctuation(self):
        got = atoms("Fixed three things:\n"
                    "- The stale claim sweep now reads claimed_at\n"
                    "- The health probe runs before any claim\n"
                    "- Attempts are counted per row")
        self.assertEqual(len(got), 3)
        self.assertTrue(all(not a.startswith("-") for a in got))

    def test_inline_code_is_never_split(self):
        got = atoms("Run `art migrate apply --dry-run` first to see what is pending.")
        self.assertEqual(got, ["Run `art migrate apply --dry-run` first to see "
                               "what is pending."])

    def test_a_fenced_block_stays_whole(self):
        text = ("Here is the fix.\n\n```sql\nSELECT 1. SELECT 2.\n```\n\n"
                "Apply it with the migration runner please.")
        self.assertTrue(any("SELECT 1. SELECT 2." in a for a in atoms(text)))

    def test_a_short_trailing_clause_joins_rather_than_vanishing(self):
        """4 words is under the gate, so splitting it would delete it. Joining
        keeps the content and costs only a coarser hash."""
        got = atoms("Run the migration to apply them. It stamps schema_migrations.")
        self.assertEqual(len(got), 1)
        self.assertIn("stamps schema_migrations", got[0])

    def test_a_long_unpunctuated_block_is_kept_whole(self):
        text = "word " * (MAX_ATOM_CHARS // 2)
        self.assertEqual(len(atoms(text)), 1)

    def test_noise_produces_no_atoms(self):
        self.assertEqual(atoms("ok thanks"), [])
        self.assertEqual(atoms(""), [])
        self.assertEqual(atoms("   "), [])

    def test_every_atom_clears_the_word_gate(self):
        for atom in atoms("Short one. This second sentence is long enough to keep."):
            self.assertGreaterEqual(len(atom.split()), MIN_ATOM_WORDS)


class NormalizeTests(unittest.TestCase):
    def test_case_and_terminal_punctuation_do_not_matter(self):
        self.assertEqual(normalize_fact("The Sweep Is Broken."),
                         normalize_fact("the sweep is broken"))

    def test_a_leading_discourse_marker_is_dropped(self):
        self.assertEqual(normalize_fact("So the sweep is broken."),
                         normalize_fact("The sweep is broken"))

    def test_whitespace_is_collapsed(self):
        self.assertEqual(normalize_fact("the   sweep\nis  broken"),
                         "the sweep is broken")

    def test_different_claims_stay_different(self):
        """Deliberately shallow: no stemming, no stopwords, no synonyms. A
        collision here is a fact silently not stored."""
        self.assertNotEqual(normalize_fact("the sweep reads claimed_at"),
                            normalize_fact("the sweep reads source_ts"))

    def test_negation_is_not_normalized_away(self):
        self.assertNotEqual(normalize_fact("the probe runs first"),
                            normalize_fact("the probe does not run first"))


class HashTests(unittest.TestCase):
    def test_the_hash_follows_normalization(self):
        self.assertEqual(fact_hash("So the sweep is broken."),
                         fact_hash("The sweep is broken"))

    def test_the_hash_is_stable_and_short_enough_to_index(self):
        self.assertEqual(len(fact_hash("anything at all")), 32)
        self.assertEqual(fact_hash("a claim"), fact_hash("a claim"))

    def test_empty_input_does_not_raise(self):
        self.assertEqual(len(fact_hash("")), 32)


class AcknowledgementFragmentTests(unittest.TestCase):
    """A trailing "Right?" is not part of the claim before it. Gluing it on makes
    that sentence hash differently from the same sentence said without it -- a
    dedupe miss caused entirely by punctuation."""

    def test_a_trailing_acknowledgement_is_dropped(self):
        self.assertEqual(
            atoms("So the health probe runs before anything is claimed.   Right?"),
            ["So the health probe runs before anything is claimed."])

    def test_it_hashes_the_same_as_the_bare_claim(self):
        with_ack = atoms("So the health probe runs before anything is claimed. Right?")[0]
        bare = atoms("The health probe runs before anything is claimed!")[0]
        self.assertEqual(fact_hash(with_ack), fact_hash(bare))

    def test_a_meaningful_short_clause_still_joins(self):
        """Only interjections are dropped; a real fragment keeps its content."""
        got = atoms("Run the migration to apply them. It stamps schema_migrations.")
        self.assertIn("stamps schema_migrations", got[0])
