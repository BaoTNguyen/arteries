"""What is worth keeping permanently.

Between the model's JSON and a permanent row there were two checks and neither
asked about worth: `validate_response` is structural, `_reject_duplicates` asks
whether we already have it. Measured consequence: 83 of 527 live rows describe
what a conversation was about to do next.
"""

import unittest

from arteries.promote import worth_keeping


class TransientIntentTests(unittest.TestCase):
    """The population the audit measured. Every example here is a real live row."""

    def test_intends_to(self):
        self.assertEqual(
            worth_keeping("User intends to merge files and update stale paths in mcp_guide.md."),
            "transient session intent")

    def test_is_considering(self):
        self.assertIsNotNone(worth_keeping(
            "User is considering replacing Ollama with OpenRouter."))

    def test_aims_to(self):
        self.assertIsNotNone(worth_keeping(
            "User aims to analyze how various coding models select context."))

    def test_wants_to_do_something(self):
        self.assertIsNotNone(worth_keeping("User wants to train the system better."))

    def test_a_leading_the_does_not_evade_the_rule(self):
        self.assertIsNotNone(worth_keeping(
            "The user intends to add 'heart' and 'plexus' to the memory harness scope."))


class DurableTests(unittest.TestCase):
    def test_a_standing_preference_is_kept(self):
        """`prefers` describes the person; `intends` describes the turn."""
        self.assertIsNone(worth_keeping(
            "User prefers using Docker Engine directly rather than Docker Desktop."))

    def test_a_preference_phrased_with_to_is_kept(self):
        self.assertIsNone(worth_keeping(
            "User prefers to install updates in a sandbox environment first."))

    def test_a_lowercase_fact_with_no_identifiers_is_kept(self):
        """An earlier draft demanded paths, digits or capitals and refused this
        one, along with 150 of 527 live rows."""
        self.assertIsNone(worth_keeping(
            "pgvector is used for retrieval in the Arteries system."))

    def test_a_plain_project_fact_is_kept(self):
        self.assertIsNone(worth_keeping(
            "Heart assigns Run IDs to Arteries for each subagent session."))


class ConversationSubjectTests(unittest.TestCase):
    def test_a_claim_about_this_session_is_refused(self):
        self.assertEqual(worth_keeping("In this session we agreed to defer the rename."),
                         "about the conversation, not the project")

    def test_a_claim_about_a_session_in_general_is_kept(self):
        """`session_id` partitions the tier -- claims about how sessions work are
        the opposite of transient."""
        self.assertIsNone(worth_keeping(
            "Ephemeral rows are keyed on session_id rather than the process id."))


class ShapeTests(unittest.TestCase):
    def test_a_fragment_is_refused(self):
        self.assertEqual(worth_keeping("done"), "too short to be a claim")

    def test_empty_input_does_not_raise(self):
        self.assertIsNotNone(worth_keeping(""))

    def test_the_reason_is_returned_not_a_bool(self):
        """Rejections are logged by rule so the filter can be argued with from
        data rather than from memory."""
        self.assertIsInstance(worth_keeping("User intends to do a thing here"), str)


class WritePathTests(unittest.TestCase):
    """The prompt asks and the code enforces. An end-to-end run showed the model
    obeying rule 5 on its own, which is the good case and leaves the enforcement
    unexercised -- so exercise it here."""

    def test_the_write_path_drops_refused_memories_before_embedding(self):
        from unittest.mock import patch

        from arteries import compile as compile_mod

        result = {"new_memories": [
            {"fact": "User intends to rewrite the compile prompt tomorrow.",
             "kind": "fact", "confidence": 0.9},
            {"fact": "The compile filter runs before embedding.",
             "kind": "fact", "confidence": 0.9},
        ]}
        embedded = []

        def _fake_embed(texts):
            embedded.extend(texts)
            return [[0.0] * 8 for _ in texts]

        events = []
        with patch("arteries.embed.embed_texts_sync", _fake_embed), \
             patch.object(compile_mod.runlog, "log_event",
                          lambda t, s, p=None, **k: events.append((t, p))), \
             patch.object(compile_mod, "_reject_duplicates",
                          side_effect=AssertionError("stop here")):
            with self.assertRaises(AssertionError):
                compile_mod._write_results(None, result, [], "p")

        self.assertEqual(embedded, ["The compile filter runs before embedding."])
        rejected = [p for t, p in events if t == "memory.compile.rejected_low_value"]
        self.assertEqual(rejected[0]["count"], 1)
        self.assertEqual(rejected[0]["by_rule"], {"transient session intent": 1})
