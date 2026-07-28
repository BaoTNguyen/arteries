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
        # remember --also-evergreen writes the same fact to persistent + evergreen
        items = [
            self._item("persistent", "Prefer scoped implementations."),
            self._item("evergreen", "prefer scoped implementations."),  # case/space dup
        ]
        out = packet._dedupe_memories(items, previous_summary="")
        self.assertEqual([i.tier for i in out], ["persistent"])

    def test_fact_already_in_previous_summary_is_dropped(self):
        items = [self._item("evergreen", "User prefers tabs over spaces.")]
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
        }])), patch.object(packet.storage, "get_evergreen", return_value=[{
            "id": "g1",
            "fact": "Prefer scoped implementations.",
            "domains": ["preference"],
            "confidence": 1.0,
        }]):
            text = packet.build_packet("manual compact", budget=4000)

        self.assertIn("## Current Context", text)
        self.assertIn("Capabilities:", text)
        self.assertIn("Recent user wants Pi first.", text)
        self.assertIn("Project integrates coding CLIs through hooks.", text)
        self.assertIn("Prefer scoped implementations.", text)
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

        with patch.object(packet.memory_select, "select_for_frame", return_value=([], [])), \
             patch.object(packet.storage, "get_evergreen", return_value=[]):
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
             patch.object(packet.storage, "get_evergreen", return_value=[]), \
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
             patch.object(packet.storage, "get_evergreen", return_value=[]), \
             patch.object(packet.runlog, "recent_events", return_value=events):
            text = packet.build_packet("manual compact", budget=4000)

        self.assertIn("Q: user asked", text)
        self.assertIn("A: assistant answered", text)

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
