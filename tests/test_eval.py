import unittest
from types import SimpleNamespace
from unittest.mock import patch

from arteries import eval as arteries_eval


class EvaluateTests(unittest.IsolatedAsyncioTestCase):
    async def test_retrieval_failure_returns_none_after_extraction(self):
        frame = SimpleNamespace(
            ephemeral=SimpleNamespace(recent_messages=[]),
            persistent=SimpleNamespace(session_insights=[]),
            scope=SimpleNamespace(sibling_insights=[]),
        )

        with patch.object(arteries_eval.runlog, "new_turn_id", return_value="turn-1"), \
             patch.object(arteries_eval.runlog, "log_event") as log_event, \
             patch.object(arteries_eval, "embed_text_sync", return_value=[0.1] * 8), \
             patch.object(arteries_eval, "extract_and_store", return_value=1) as extract_and_store, \
             patch.object(arteries_eval, "get_current_frame", return_value=frame), \
             patch.object(arteries_eval, "run_gate", side_effect=RuntimeError("embed unavailable")), \
             patch.object(arteries_eval, "_spawn_detached_compile"):  # fire-and-forget, now synchronous
            result = await arteries_eval.evaluate("I prefer stable hooks")

        self.assertIsNone(result)
        extract_and_store.assert_called_once_with(
            "I prefer stable hooks", embedding=[0.1] * 8
        )
        first_event = log_event.call_args_list[0]
        self.assertEqual(first_event.args[0], "turn.observed")
        self.assertEqual(first_event.args[2]["message_preview"], "I prefer stable hooks")
        self.assertEqual(first_event.args[2]["message_chars"], 21)
        self.assertIn("message_sha256", first_event.args[2])
        self.assertGreaterEqual(log_event.call_count, 3)


if __name__ == "__main__":
    unittest.main()


class TurnEmbeddingTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_embed_call_shared_by_writes_and_reads(self):
        """The turn is embedded once, and both the write and the read use it.

        Embedding per extracted record instead would put N calls on the hook
        path; embedding separately for extract and for frame would put two.
        """
        frame = SimpleNamespace(
            ephemeral=SimpleNamespace(recent_messages=[]),
            persistent=SimpleNamespace(session_insights=[]),
            scope=SimpleNamespace(sibling_insights=[]),
        )
        vec = [0.5] * 8

        with patch.object(arteries_eval.runlog, "new_turn_id", return_value="turn-2"), \
             patch.object(arteries_eval.runlog, "log_event"), \
             patch.object(arteries_eval, "embed_text_sync", return_value=vec) as embed, \
             patch.object(arteries_eval, "extract_and_store", return_value=2) as extract, \
             patch.object(arteries_eval, "get_current_frame", return_value=frame) as build, \
             patch.object(arteries_eval, "run_gate", side_effect=RuntimeError("stop here")), \
             patch.object(arteries_eval, "_spawn_detached_compile"):
            await arteries_eval.evaluate("we migrated arteries to pgvector 1024")

        self.assertEqual(embed.call_count, 1)
        self.assertTrue(embed.call_args.kwargs.get("is_query"))
        self.assertEqual(extract.call_args.kwargs["embedding"], vec)
        self.assertEqual(build.call_args.kwargs["embedding"], vec)
