import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from arteries import frame


class BuildFrameTests(unittest.TestCase):
    @patch.object(frame.storage, "get_recent_retrievals")
    @patch.object(frame.storage, "get_active_domains")
    @patch.object(frame.memory_select, "select_for_frame")
    def test_builds_all_memory_tiers(
        self,
        select_for_frame,
        get_active_domains,
        get_recent_retrievals,
    ):
        select_for_frame.return_value = ([
            {"fact": "User is testing the sync extractor", "domains": ["technical"], "confidence": 0.7},
            {"fact": "User wants RLVR for memory promotion", "domains": ["AI"], "confidence": 0.8},
        ], [
            {"fact": "Project uses capillaries MemoryFrame", "domains": ["technical"], "confidence": 0.9},
        ])
        get_active_domains.return_value = ["technical"]
        get_recent_retrievals.return_value = [
            {
                "prompt_id": "rlvr-harness",
                "situation": "testing memory promotion",
                "score": 0.91,
                "relevance": 0.75,
                "created_at": datetime(2026, 6, 23, tzinfo=timezone.utc),
            }
        ]

        result = frame._build_frame("How should I test memory?")

        self.assertEqual(result.ephemeral.recent_messages, [
            "User is testing the sync extractor",
            "User wants RLVR for memory promotion",
        ])
        self.assertEqual(result.ephemeral.turn_count, 2)
        self.assertEqual(result.ephemeral.topic_drift, 0.5)

        self.assertEqual(result.persistent.active_domains, ["technical"])
        self.assertEqual(result.persistent.session_insights[0].text, "Project uses capillaries MemoryFrame")
        self.assertEqual(result.persistent.session_insights[0].domain, "technical")
        self.assertEqual(result.persistent.prior_retrievals[0].prompt_id, "rlvr-harness")

        self.assertEqual(result.scope.user_intent, [])
        self.assertEqual(result.scope.recurring_domains, [])
        self.assertEqual(result.scope.sibling_insights, [])
        self.assertEqual(result.scope.retrieval_confidence, 0.91)

    @patch.object(frame, "_build_frame", side_effect=RuntimeError("db unavailable"))
    def test_get_current_frame_falls_back_to_empty_frame(self, _build_frame):
        result = frame.get_current_frame("anything")

        self.assertEqual(result.ephemeral.recent_messages, [])
        self.assertEqual(result.persistent.session_insights, [])
        self.assertEqual(result.scope.sibling_insights, [])


if __name__ == "__main__":
    unittest.main()
