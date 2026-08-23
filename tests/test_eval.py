import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from arteries import eval as arteries_eval


class EvaluateTests(unittest.IsolatedAsyncioTestCase):
    def test_triage_does_not_skip_for_memory_subject_overlap(self):
        reason = arteries_eval._triage_skip_reason(
            "Set up tier A so I can start labeling",
            ["Tier A labels whether the corpus covers the query."],
        )
        self.assertIsNone(reason)

    def test_triage_skips_explicit_continuation_of_assistant_result(self):
        reason = arteries_eval._triage_skip_reason(
            "Revise the previous labeling plan",
            ["Here is the Tier A labeling plan."],
        )
        self.assertEqual(reason, "explicit continuation of prior assistant result")

    def test_assistant_ephemeral_is_valid_continuation_evidence(self):
        evidence = arteries_eval._assistant_ephemeral_text([
            {"fact": "The user asked for a SaaS product strategy.", "source": "user"},
            {"fact": "Here is the Tier A labeling plan.", "source": "assistant"},
        ])
        self.assertEqual(evidence, ["Here is the Tier A labeling plan."])
        self.assertEqual(
            arteries_eval._triage_skip_reason("Revise the previous labeling plan", evidence),
            "explicit continuation of prior assistant result",
        )

    def test_triage_allows_unresolved_request(self):
        self.assertIsNone(arteries_eval._triage_skip_reason(
            "build a 13-week cash flow model for a SaaS startup", []
        ))

    def test_triage_allows_fresh_direct_instruction_without_context(self):
        self.assertIsNone(arteries_eval._triage_skip_reason(
            "Set up tier A so I can start labeling", []
        ))

    def test_triage_allows_subject_overlap_without_explicit_continuation(self):
        self.assertIsNone(arteries_eval._triage_skip_reason(
            "create a pricing and packaging strategy for a SaaS product",
            ["Here is a go-to-market strategy for a B2B SaaS product."],
        ))

    def test_triage_allows_explicit_workflow_question(self):
        self.assertIsNone(arteries_eval._triage_skip_reason(
            "How should I structure an incident response workflow?", []
        ))

    def test_placeholder_prompt_is_wrapped_without_inventing_values(self):
        prompt, placeholders = arteries_eval._prepare_injection(
            "Build a model for [COMPANY] using {{ revenue data }}."
        )
        self.assertEqual(placeholders, ["COMPANY", "revenue data"])
        self.assertIn("do not invent values", prompt)
        self.assertTrue(prompt.endswith("{{ revenue data }}."))

    async def test_explicit_continuation_does_not_call_capillaries(self):
        frame = SimpleNamespace(
            ephemeral=SimpleNamespace(recent_messages=[]),
            persistent=SimpleNamespace(session_insights=[]),
            evergreen=SimpleNamespace(ground_truth_insights=[]),
        )
        with patch.object(arteries_eval.runlog, "new_turn_id", return_value="turn-1"), \
             patch.object(arteries_eval.runlog, "log_event"), \
             patch.object(arteries_eval, "extract_and_store", return_value=1), \
             patch.object(arteries_eval, "get_current_frame", return_value=frame), \
             patch.object(arteries_eval, "recent_assistant_turns", return_value=["Here is the Tier A labeling plan."]), \
             patch.object(arteries_eval.memory_select, "select_ephemeral", return_value=[]), \
             patch.object(arteries_eval, "cap_find", new_callable=AsyncMock) as cap_find, \
             patch.object(arteries_eval, "_spawn_detached_compile"):
            result = await arteries_eval.evaluate("Revise the previous labeling plan")

        self.assertIsNone(result)
        cap_find.assert_not_awaited()

    async def test_retrieval_failure_returns_none_after_extraction(self):
        frame = SimpleNamespace(
            ephemeral=SimpleNamespace(recent_messages=[]),
            persistent=SimpleNamespace(session_insights=[]),
            evergreen=SimpleNamespace(ground_truth_insights=[]),
        )

        with patch.object(arteries_eval.runlog, "new_turn_id", return_value="turn-1"), \
             patch.object(arteries_eval.runlog, "log_event") as log_event, \
             patch.object(arteries_eval, "extract_and_store", return_value=1) as extract_and_store, \
             patch.object(arteries_eval, "get_current_frame", return_value=frame), \
             patch.object(arteries_eval.memory_select, "select_ephemeral", return_value=[]), \
             patch.object(arteries_eval, "cap_find", side_effect=RuntimeError("retrieve unavailable")), \
             patch.object(arteries_eval, "_spawn_detached_compile"):  # fire-and-forget, now synchronous
            result = await arteries_eval.evaluate("I prefer stable hooks")

        self.assertIsNone(result)
        extract_and_store.assert_called_once_with("I prefer stable hooks")
        first_event = log_event.call_args_list[0]
        self.assertEqual(first_event.args[0], "turn.observed")
        self.assertEqual(first_event.args[2]["message_preview"], "I prefer stable hooks")
        self.assertEqual(first_event.args[2]["message_chars"], 21)
        self.assertIn("message_sha256", first_event.args[2])
        self.assertGreaterEqual(log_event.call_count, 3)


if __name__ == "__main__":
    unittest.main()
