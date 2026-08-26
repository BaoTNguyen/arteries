import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from arteries import packet


class DedupeTests(unittest.TestCase):
    def _item(self, tier, text):
        return packet.MemoryItem(tier=tier, text=text, confidence=1.0, domains=[])

    def test_same_fact_across_tiers_kept_once_first_tier_wins(self):
        items = [
            self._item("persistent", "Prefer scoped implementations."),
            self._item("persistent", "prefer scoped implementations."),  # case/space dup
        ]
        out = packet._dedupe_memories(items, previous_summary="")
        self.assertEqual([i.tier for i in out], ["persistent"])

    def test_fact_already_in_previous_summary_is_dropped(self):
        items = [self._item("persistent", "User prefers tabs over spaces.")]
        out = packet._dedupe_memories(
            items, previous_summary=packet._norm("Earlier: user prefers tabs over spaces, noted."))
        self.assertEqual(out, [])

    def test_status_lines_survive_dedup(self):
        items = [self._item("status", "Memory storage was unavailable.")]
        out = packet._dedupe_memories(items, previous_summary="memory storage was unavailable.")
        self.assertEqual(len(out), 1)


class PacketTests(unittest.TestCase):
    def test_build_packet_includes_memory_tiers_and_rules(self):
        with patch.object(packet.memory_select, "select_for_frame", return_value=([{
            "id": "e1",
            "fact": "Recent user wants Pi first.",
            "domains": ["context"],
            "confidence": 0.8,
        }], [{
            "id": "p1",
            "fact": "Project integrates coding CLIs through hooks.",
            "domains": ["technical"],
            "confidence": 0.9,
        }])):
            text = packet.build_packet("manual compact", budget=4000)

        self.assertIn("## Current Context", text)
        self.assertIn("Capabilities:", text)
        self.assertIn("Recent user wants Pi first.", text)
        self.assertIn("Project integrates coding CLIs through hooks.", text)
        self.assertIn("Treat this packet as continuity context", text)


    def test_build_packet_includes_recent_pairs_from_event_and_caps_at_ten(self):
        event = {
            "messages": [
                item
                for idx in range(11)
                for item in (
                    {"role": "user", "content": f"question {idx}"},
                    {"role": "assistant", "content": f"answer {idx}"},
                )
            ]
        }

        with patch.object(packet.memory_select, "select_for_frame", return_value=([], [])):
            text = packet.build_packet("auto compact", event=event, budget=8000)

        self.assertIn("## Recent Conversation", text)
        self.assertNotIn("question 0", text)
        self.assertIn("question 1", text)
        self.assertIn("answer 10", text)
        self.assertEqual(text.count("Q: question"), 10)

    def test_build_packet_uses_runlog_user_turns_without_fabricating_answers(self):
        events = [{
            "event_type": "turn.observed",
            "turn_id": "turn-1",
            "payload": {"message_preview": "user asked for setup"},
            "created_at": "2026-01-01T00:00:00Z",
        }]

        with patch.object(packet.memory_select, "select_for_frame", return_value=([], [])), \
             patch.object(packet.runlog, "recent_events", return_value=events):
            text = packet.build_packet("manual compact", budget=4000)

        self.assertIn("Q: user asked for setup", text)
        self.assertIn("A: [not captured by this CLI]", text)

    def test_build_packet_can_pair_assistant_events_from_runlog(self):
        events = [
            {
                "event_type": "assistant.response",
                "turn_id": "turn-1",
                "payload": {"assistant_preview": "assistant answered"},
                "created_at": "2026-01-01T00:00:01Z",
            },
            {
                "event_type": "turn.observed",
                "turn_id": "turn-1",
                "payload": {"message_preview": "user asked"},
                "created_at": "2026-01-01T00:00:00Z",
            },
        ]

        with patch.object(packet.memory_select, "select_for_frame", return_value=([], [])), \
             patch.object(packet.runlog, "recent_events", return_value=events):
            text = packet.build_packet("manual compact", budget=4000)

        self.assertIn("Q: user asked", text)
        self.assertIn("A: assistant answered", text)

    def test_recent_conversation_excludes_other_sessions(self):
        events = [
            {"event_type": "turn.observed", "turn_id": "t3",
             "payload": {"message_preview": "mine again", "session_id": "s-me"}},
            {"event_type": "turn.observed", "turn_id": "t2",
             "payload": {"message_preview": "someone else's turn", "session_id": "s-other"}},
            {"event_type": "turn.observed", "turn_id": "t1",
             "payload": {"message_preview": "mine", "session_id": "s-me"}},
        ]

        with patch.object(packet.memory_select, "select_for_frame", return_value=([], [])), \
             patch.object(packet.runlog, "recent_events", return_value=events), \
             patch.dict("os.environ", {"ARTERIES_SESSION_ID": "s-me"}):
            text = packet.build_packet("manual compact", budget=4000)

        self.assertIn("1. Q: mine", text)
        self.assertIn("2. Q: mine again", text)
        self.assertNotIn("someone else", text)

    def test_rows_without_a_session_are_kept(self):
        # everything written before session stamping. Filtering these out would
        # render an empty packet on an existing store.
        events = [
            {"event_type": "turn.observed", "turn_id": "t1",
             "payload": {"message_preview": "an older turn"}},
        ]

        with patch.object(packet.memory_select, "select_for_frame", return_value=([], [])), \
             patch.object(packet.runlog, "recent_events", return_value=events), \
             patch.dict("os.environ", {"ARTERIES_SESSION_ID": "s-me"}):
            text = packet.build_packet("manual compact", budget=4000)

        self.assertIn("Q: an older turn", text)

    def test_no_session_in_env_filters_nothing(self):
        events = [
            {"event_type": "turn.observed", "turn_id": "t1",
             "payload": {"message_preview": "a turn", "session_id": "s-other"}},
        ]

        env = {k: v for k, v in __import__("os").environ.items() if k != "ARTERIES_SESSION_ID"}
        with patch.object(packet.memory_select, "select_for_frame", return_value=([], [])), \
             patch.object(packet.runlog, "recent_events", return_value=events), \
             patch.dict("os.environ", env, clear=True):
            text = packet.build_packet("manual compact", budget=4000)

        self.assertIn("Q: a turn", text)

    def test_join_key_beats_position_when_a_capture_was_skipped(self):
        # turn-2's answer was never captured. Counting backwards would slide
        # turn-3's answer onto turn-2 and every earlier pair with it.
        events = [
            {"event_type": "assistant.response", "turn_id": "turn-4",
             "payload": {"assistant_preview": "answer about caching",
                         "answers_preview": "how does caching work", "prior_turn": True}},
            {"event_type": "turn.observed", "turn_id": "turn-4",
             "payload": {"message_preview": "fourth question"}},
            {"event_type": "turn.observed", "turn_id": "turn-3",
             "payload": {"message_preview": "how does caching work"}},
            {"event_type": "turn.observed", "turn_id": "turn-2",
             "payload": {"message_preview": "an unanswered question"}},
        ]

        with patch.object(packet.memory_select, "select_for_frame", return_value=([], [])), \
             patch.object(packet.runlog, "recent_events", return_value=events):
            text = packet.build_packet("manual compact", budget=4000)

        self.assertIn("1. Q: an unanswered question\n   A: [not captured", text)
        self.assertIn("2. Q: how does caching work\n   A: answer about caching", text)

    def test_join_key_picks_the_live_copy_of_a_resubmitted_prompt(self):
        events = [
            {"event_type": "assistant.response", "turn_id": "turn-3",
             "payload": {"assistant_preview": "the answer",
                         "answers_preview": "same question", "prior_turn": True}},
            {"event_type": "turn.observed", "turn_id": "turn-3",
             "payload": {"message_preview": "later question"}},
            {"event_type": "turn.observed", "turn_id": "turn-2",
             "payload": {"message_preview": "same question"}},
            {"event_type": "turn.observed", "turn_id": "turn-1",
             "payload": {"message_preview": "same question"}},
        ]

        with patch.object(packet.memory_select, "select_for_frame", return_value=([], [])), \
             patch.object(packet.runlog, "recent_events", return_value=events):
            text = packet.build_packet("manual compact", budget=4000)

        self.assertIn("1. Q: same question\n   A: [not captured", text)
        self.assertIn("2. Q: same question\n   A: the answer", text)

    def test_truncated_join_key_still_matches_a_longer_question(self):
        events = [
            {"event_type": "assistant.response", "turn_id": "turn-2",
             "payload": {"assistant_preview": "the answer",
                         "answers_preview": "explain the retry", "prior_turn": True}},
            {"event_type": "turn.observed", "turn_id": "turn-2",
             "payload": {"message_preview": "next"}},
            {"event_type": "turn.observed", "turn_id": "turn-1",
             "payload": {"message_preview": "explain the retry policy in detail please"}},
        ]

        with patch.object(packet.memory_select, "select_for_frame", return_value=([], [])), \
             patch.object(packet.runlog, "recent_events", return_value=events):
            text = packet.build_packet("manual compact", budget=4000)

        self.assertIn("1. Q: explain the retry policy in detail please\n   A: the answer", text)

    def test_stop_capture_attaches_to_its_own_turn(self):
        # Stop fires at the end of turn N and the transcript's last assistant
        # message is turn N's answer -- no prior_turn, no offset
        events = [
            {"event_type": "assistant.response", "turn_id": "turn-2",
             "payload": {"assistant_preview": "second answer"}},
            {"event_type": "turn.observed", "turn_id": "turn-2",
             "payload": {"message_preview": "second question"}},
            {"event_type": "assistant.response", "turn_id": "turn-1",
             "payload": {"assistant_preview": "first answer"}},
            {"event_type": "turn.observed", "turn_id": "turn-1",
             "payload": {"message_preview": "first question"}},
        ]

        with patch.object(packet.memory_select, "select_for_frame", return_value=([], [])), \
             patch.object(packet.runlog, "recent_events", return_value=events):
            text = packet.build_packet("manual compact", budget=4000)

        self.assertIn("1. Q: first question\n   A: first answer", text)
        self.assertIn("2. Q: second question\n   A: second answer", text)

    def test_later_capture_of_the_same_turn_replaces_the_earlier_one(self):
        events = [
            {"event_type": "assistant.response", "turn_id": "turn-1",
             "payload": {"assistant_preview": "the complete answer"}},
            {"event_type": "assistant.response", "turn_id": "turn-1",
             "payload": {"assistant_preview": "partial"}},
            {"event_type": "turn.observed", "turn_id": "turn-1",
             "payload": {"message_preview": "a question"}},
        ]

        with patch.object(packet.memory_select, "select_for_frame", return_value=([], [])), \
             patch.object(packet.runlog, "recent_events", return_value=events):
            text = packet.build_packet("manual compact", budget=4000)

        self.assertIn("A: the complete answer", text)
        self.assertNotIn("partial", text)

    def test_prior_turn_capture_attaches_to_the_turn_it_describes(self):
        # the capture for turn-2 describes turn-1 and lands before turn-2's own
        # turn.observed row -- the case that used to staple it onto turn-1's
        # neighbour instead
        events = [
            {"event_type": "turn.observed", "turn_id": "turn-2",
             "payload": {"message_preview": "second question"}},
            {"event_type": "assistant.response", "turn_id": "turn-2",
             "payload": {"assistant_preview": "answer to first", "prior_turn": True}},
            {"event_type": "turn.observed", "turn_id": "turn-1",
             "payload": {"message_preview": "first question"}},
        ]

        with patch.object(packet.memory_select, "select_for_frame", return_value=([], [])), \
             patch.object(packet.runlog, "recent_events", return_value=events):
            text = packet.build_packet("manual compact", budget=4000)

        self.assertIn("1. Q: first question\n   A: answer to first", text)
        self.assertIn("2. Q: second question\n   A: [not captured", text)

    def test_prior_turn_capture_outside_the_window_is_dropped(self):
        events = [
            {"event_type": "assistant.response", "turn_id": "turn-1",
             "payload": {"assistant_preview": "answer to turn zero", "prior_turn": True}},
            {"event_type": "turn.observed", "turn_id": "turn-1",
             "payload": {"message_preview": "only question"}},
        ]

        with patch.object(packet.memory_select, "select_for_frame", return_value=([], [])), \
             patch.object(packet.runlog, "recent_events", return_value=events):
            text = packet.build_packet("manual compact", budget=4000)

        self.assertNotIn("answer to turn zero", text)

    def test_recent_section_keeps_all_ten_pairs_at_full_length(self):
        long = "x" * 600
        event = {"messages": [
            item for idx in range(10)
            for item in ({"role": "user", "content": f"question {idx} {long}"},
                         {"role": "assistant", "content": f"answer {idx} {long}"})
        ]}

        with patch.object(packet.memory_select, "select_for_frame", return_value=([], [])):
            text = packet.build_packet("auto compact", event=event)

        self.assertNotIn("[Section truncated to fit budget.]", text)
        self.assertEqual(text.count("Q: question"), 10)

    def test_pi_json_format_wraps_summary(self):
        out = io.StringIO()
        with patch.object(packet, "build_packet", return_value="summary"):
            with redirect_stdout(out):
                self.assertEqual(packet.main(["--format", "pi-compaction-json", "--message", "auto"]), 0)

        payload = json.loads(out.getvalue())
        self.assertEqual(payload["summary"], "summary")
        self.assertEqual(payload["details"]["source"], "arteries")
        self.assertIn("cli_capabilities", payload["details"])


if __name__ == "__main__":
    unittest.main()


class FloorCalibrationTests(unittest.TestCase):
    """The floor has to sit above the model's noise band.

    Measured over 237 real plexus queries against 115 claims: top-hit
    similarity runs p25 0.50, p50 0.55, p90 0.66. Qwen3-Embedding-0.6B puts
    unrelated technical prose at roughly 0.45-0.55, so the old 0.45 admitted the
    25th percentile of everything -- which is how a question about cost tracking
    returned claims about retrieval ownership.
    """

    def test_floor_sits_above_the_noise_band(self):
        self.assertGreaterEqual(packet.MEMORY_SIMILARITY_FLOOR, 0.55)

    def test_a_row_below_the_floor_is_refused(self):
        self.assertIsNone(packet._score("persistent", {"similarity": 0.50, "confidence": 1.0}))

    def test_a_row_above_the_floor_is_admitted(self):
        self.assertIsNotNone(packet._score("persistent", {"similarity": 0.70, "confidence": 1.0}))

    def test_rows_without_a_similarity_are_judged_by_another_policy(self):
        """Ephemeral is selected by session and recency, not by distance, so it
        has no similarity to be measured against this floor."""
        self.assertIsNotNone(packet._score("ephemeral", {"confidence": 1.0}))
