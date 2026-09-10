import io
import json
import os
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

    @staticmethod
    def _ten_long_pairs():
        long = "x" * 600
        return {"messages": [
            item for idx in range(10)
            for item in ({"role": "user", "content": f"question {idx} {long}"},
                         {"role": "assistant", "content": f"answer {idx} {long}"})
        ]}

    def test_recent_section_keeps_all_ten_pairs_when_it_is_the_compaction(self):
        """`pi` replaces the host's compaction output, so these turns are the
        only record of the conversation and truncating them loses the thing
        being compacted."""
        with patch.dict(os.environ, {"ARTERIES_CLI": "pi"}), \
             patch.object(packet.memory_select, "select_for_frame", return_value=([], [])):
            text = packet.build_packet("auto compact", event=self._ten_long_pairs())

        self.assertNotIn("[Section truncated to fit budget.]", text)
        self.assertEqual(text.count("Q: question"), 10)

    def test_the_injection_path_does_not_re_send_the_conversation(self):
        """Finding 18. `claude` keeps its own transcript, so spending 55% of the
        packet repeating it back was 55% not spent on memory."""
        with patch.dict(os.environ, {"ARTERIES_CLI": "claude"}), \
             patch.object(packet.memory_select, "select_for_frame", return_value=([], [])):
            text = packet.build_packet("auto compact", event=self._ten_long_pairs())

        self.assertLess(text.count("Q: question"), 10)

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


class ProvenanceTests(unittest.TestCase):
    """What went into the packet, so an episode outcome can be attributed to it.

    After rank fusion the number a row carries is not comparable across runs --
    RRF scores depend on how many rows each arm happened to return -- so the
    record is the row's position, not its score.
    """

    def _prov(self, ephemerals, persistents, message="a real question"):
        from unittest.mock import patch

        prov = []
        with patch.object(packet, "recent_assistant_turns", return_value=[]), \
             patch.object(packet, "embed_text_sync", return_value=[0.0] * 8), \
             patch.object(packet.memory_select, "select_for_frame",
                          return_value=(ephemerals, persistents)):
            packet._load_memories(message, provenance=prov)
        return prov

    def test_provenance_records_rank_and_arm(self):
        prov = self._prov([], [{"id": "p1", "fact": "a fact", "similarity": 0.8,
                                "confidence": 1.0}])
        self.assertEqual(prov[0]["tier"], "persistent")
        self.assertEqual(prov[0]["rank"], 1)

    def test_a_graph_row_is_recorded_under_its_own_arm(self):
        """It renders in the Persistent section but was ranked as `related`;
        attribution needs the arm, not the heading."""
        prov = self._prov([], [{"id": "g1", "fact": "reached", "similarity": 0.3,
                                "confidence": 1.0, "via_graph": True}])
        self.assertEqual(prov[0]["tier"], "related")

    def test_episode_and_task_ids_survive(self):
        prov = self._prov([], [{"id": "p1", "fact": "a fact", "similarity": 0.8,
                                "confidence": 1.0, "episode_id": "e9", "task_id": "t9"}])
        self.assertEqual((prov[0]["episode_id"], prov[0]["task_id"]), ("e9", "t9"))

    def test_a_skipped_turn_still_records_its_ephemeral(self):
        """A triage skip invalidates similarity, not recency -- so a
        continuation turn still has a working set, and attribution still needs
        to know what it saw."""
        prov = self._prov([{"id": "e1", "fact": "recent", "confidence": 1.0}], [],
                          message="yes")
        self.assertEqual([(p["tier"], p["rank"]) for p in prov], [("ephemeral", 1)])


class ConfidenceIsAnAnnotationTests(unittest.TestCase):
    """Finding 4. 448 of 527 live rows sit at 0.9 or above, so multiplying
    relevance by confidence was a constant for 85% of the store and a silent
    demotion for the rest."""

    def test_ordering_does_not_depend_on_confidence(self):
        sure = {"id": "a", "similarity": 0.70, "confidence": 1.0}
        unsure = {"id": "b", "similarity": 0.72, "confidence": 0.7}
        arms = dict(packet._arms([], [sure, unsure]))
        self.assertEqual([r["id"] for r in arms["persistent"]], ["b", "a"],
                         "the more relevant row should win regardless of certainty")

    def test_the_floor_still_applies(self):
        arms = dict(packet._arms([], [{"id": "x", "similarity": 0.4, "confidence": 1.0}]))
        self.assertEqual(arms["persistent"], [])

    def test_confidence_still_reaches_the_reader(self):
        """Stored and rendered, so a 0.7 claim can be discounted by whoever
        reads it rather than by the ranker."""
        items = packet._rows("persistent", [
            {"id": "p", "fact": "a shaky claim", "confidence": 0.7}])
        self.assertIn("conf=0.70", packet._format_items(items, "persistent")[0])


class BudgetTests(unittest.TestCase):
    """Finding 18: `Recent Conversation` took 55% of the budget on every path,
    and it is the one section every host CLI keeps verbatim."""

    def _caps(self, **kw):
        from arteries.cli_caps import CliCapabilities

        return CliCapabilities(name="test", **kw)

    def test_injecting_spends_the_budget_on_memory(self):
        a = packet._allocations(20000, self._caps())
        self.assertGreater(a["memory"] * 2, a["recent"])

    def test_replacing_compaction_keeps_the_conversation(self):
        """When the packet *is* the compaction output, the turns are the only
        record of what happened."""
        a = packet._allocations(20000, self._caps(can_replace_compaction=True))
        self.assertGreater(a["recent"], a["memory"] * 2)

    def test_shares_stay_under_the_hard_limit(self):
        """Over-allocating hands the decision back to truncation, which is what
        ranking exists to take away from it. The old shares summed to 1.08."""
        for caps in (self._caps(), self._caps(can_replace_compaction=True)):
            a = packet._allocations(20000, caps)
            total = a["context"] + a["recent"] + a["memory"] * 2 + a["suggestion"] + a["rules"]
            self.assertLessEqual(total, 20000)

    def test_a_tiny_budget_does_not_go_negative(self):
        a = packet._allocations(1, self._caps())
        self.assertTrue(all(v >= 0 for v in a.values()))
