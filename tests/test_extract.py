"""Turn capture and assistant compression."""

import unittest

from arteries.extract import (
    MIN_EXTRACTABLE_WORDS,
    extract_from_message,
    strip_assistant_response,
)


class ExtractFromMessageTests(unittest.TestCase):
    def test_a_turn_is_stored_whole(self):
        """No truncation. The old fallback cut every message at 500 characters,
        which was 87% of the corpus losing its tail."""
        message = "we use pgvector. " * 60          # ~1020 chars
        (extraction,) = extract_from_message(message)
        self.assertEqual(extraction.fact, message)
        self.assertGreater(len(extraction.fact), 500)

    def test_one_record_per_turn(self):
        out = extract_from_message("I prefer stdlib. We use Postgres. No, actually pgvector.")
        self.assertEqual(len(out), 1)

    def test_short_turns_are_skipped(self):
        self.assertEqual(extract_from_message("ok thanks"), [])
        self.assertEqual(extract_from_message(" ".join(["w"] * (MIN_EXTRACTABLE_WORDS - 1))), [])

    def test_domains_are_still_inferred(self):
        (extraction,) = extract_from_message(
            "we run Postgres with pgvector for the arteries database schema"
        )
        self.assertIn("technical", extraction.domains)


class AssistantCompressionTests(unittest.TestCase):
    def _long_reply(self):
        opening = "The compile pass takes 16.9s because generation dominates prefill.\n"
        narration = "\n".join(f"Let me check the next thing, step {i}." for i in range(60))
        facts = "\n".join(f"`table_{i}.column_{i}` holds {i * 7} rows." for i in range(20))
        filler = "\n".join("Prose that names nothing and simply continues." for _ in range(60))
        closing = "So `_write_results` must batch embeddings before the transaction opens."
        return "\n".join([opening, narration, facts, filler, closing])

    def test_both_ends_survive_compression(self):
        """Conclusions live at the end of an assistant reply. Head-only
        truncation threw away the part worth keeping."""
        out = strip_assistant_response(self._long_reply())
        self.assertIn("generation dominates prefill", out)
        self.assertIn("_write_results` must batch", out)
        self.assertLess(len(out), 2500)

    def test_narration_and_code_fences_are_dropped(self):
        out = strip_assistant_response(
            "Let me check that for you.\n```python\nprint('x')\n```\n"
            "`compile.py` claims 10 rows per pass."
        )
        self.assertNotIn("Let me check", out)
        self.assertNotIn("print('x')", out)
        self.assertIn("claims 10 rows", out)

    def test_restatement_of_the_question_is_dropped(self):
        user = "how does the scope CTE resolve sibling projects for a read"
        out = strip_assistant_response(
            "How does the scope CTE resolve sibling projects for a read?\n"
            "It self-joins `scope_members` on `scope_id`.",
            user_turn=user,
        )
        self.assertNotIn("How does the scope CTE resolve", out)
        self.assertIn("self-joins", out)

    def test_short_replies_pass_through_untouched(self):
        out = strip_assistant_response("`origin` replaced `scope` on the persistent table.")
        self.assertEqual(out, "`origin` replaced `scope` on the persistent table.")


if __name__ == "__main__":
    unittest.main()
