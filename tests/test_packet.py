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


class RankFusionTests(unittest.TestCase):
    """Tiers are fused by rank because their scores are not the same quantity.

    Before this, `_score` compared a persistent cosine against ephemeral's flat
    NEUTRAL_SIMILARITY and a graph neighbour's decayed hop score. Both
    comparisons were arithmetically lost before they started -- see TIER_WEIGHT.
    """

    @staticmethod
    def _eph(n):
        return [{"id": f"e{i}", "fact": f"eph {i}", "confidence": 1.0} for i in range(n)]

    @staticmethod
    def _per(n, top=0.75):
        return [{"id": f"p{i}", "fact": f"per {i}", "confidence": 0.9,
                 "similarity": top - 0.01 * i} for i in range(n)]

    def _packet_arms(self, eph, per):
        return [arm for arm, _row in
                packet._fuse(packet._arms(eph, per))[:packet.MAX_PACKET_MEMORIES]]

    def test_persistent_reaches_the_packet_when_ephemeral_is_full(self):
        """The regression this commit exists for.

        20 ephemeral candidates for 15 slots, and persistent at the *measured*
        median top hit of 0.55. Under score comparison a 0.90-confidence row
        needed 0.585 to tie a flat 0.5 ephemeral row, so persistent contributed
        nothing at all on a median query.
        """
        arms = self._packet_arms(self._eph(20), self._per(20, top=0.55))
        self.assertGreater(arms.count("persistent"), 0)

    def test_a_tier_with_nothing_relevant_takes_no_slots(self):
        """No preassigned allocation: the mix follows what each arm has."""
        arms = self._packet_arms(self._eph(20), [])
        self.assertEqual(set(arms), {"ephemeral"})

    def test_graph_rows_are_not_judged_by_the_cosine_floor(self):
        """Finding 26. A neighbour scores decay x seed_similarity -- at most
        0.6 x 0.78 = 0.47 with the measured ceiling, so a 0.55 cosine floor
        refused every one of them, always."""
        reached = {"id": "g0", "fact": "reached", "confidence": 0.9,
                   "similarity": 0.31, "via_graph": True, "via": "contradicts"}
        arms = dict(packet._arms([], [reached]))
        self.assertEqual([r["id"] for r in arms["related"]], ["g0"])

    def test_graph_rows_are_bounded_instead(self):
        reached = [{"id": f"g{i}", "fact": f"g{i}", "confidence": 0.9,
                    "similarity": 0.3, "via_graph": True} for i in range(9)]
        arms = dict(packet._arms([], reached))
        self.assertEqual(len(arms["related"]), packet.MAX_GRAPH_MEMORIES)

    def test_fusion_is_stable(self):
        eph, per = self._eph(6), self._per(6)
        first = [r["id"] for _a, r in packet._fuse(packet._arms(eph, per))]
        second = [r["id"] for _a, r in packet._fuse(packet._arms(eph, per))]
        self.assertEqual(first, second)


class ContradictionLabellingTests(unittest.TestCase):
    """Finding 15: `graph.expand` computes `via`, `_rows` used to drop it, and
    the packet presented a claim and its contradiction as identical bullets."""

    def test_via_survives_into_the_memory_item(self):
        item = packet._rows("persistent", [
            {"id": "g0", "fact": "the opposite is true", "confidence": 0.9,
             "via": "contradicts"}])[0]
        self.assertEqual(item.via, "contradicts")

    def test_a_contradiction_renders_marked(self):
        items = packet._rows("persistent", [
            {"id": "p0", "fact": "the build is green", "confidence": 0.9},
            {"id": "g0", "fact": "the build is red", "confidence": 0.9,
             "via": "contradicts"}])
        lines = packet._format_items(items, "persistent")
        self.assertNotIn("[", lines[0])
        self.assertTrue(lines[1].startswith("- [contradicts] "))

    def test_a_direct_hit_is_not_marked(self):
        item = packet._rows("persistent", [
            {"id": "p0", "fact": "plain", "confidence": 0.9}])[0]
        self.assertIsNone(item.via)
