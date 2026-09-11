"""Retrieval only runs when there is something to retrieve.

The rules already gated the prompt-corpus search on the hook path. Memory
retrieval ran unconditionally, so on a turn like "yes" or "clean up" arteries
embedded a non-query and then ranked the results of searching for it -- and a
nearest-neighbour search always has a nearest neighbour.
"""

import unittest
from unittest.mock import patch

from arteries import memory_select, packet, triage


class SkipRuleTests(unittest.TestCase):
    def test_an_acknowledgement_has_nothing_to_search_for(self):
        self.assertEqual(triage.skip_reason("yes", []), "acknowledgement")
        self.assertEqual(triage.skip_reason("sounds good!", []), "acknowledgement")

    def test_a_bare_imperative_takes_its_object_from_the_turn_before(self):
        self.assertIsNotNone(triage.skip_reason("clean up", ["a prior answer"]))

    def test_a_bare_imperative_with_no_prior_turn_is_a_real_request(self):
        """Nothing to continue means it must mean itself."""
        self.assertIsNone(triage.skip_reason("clean up", []))

    def test_a_question_naming_its_own_subject_retrieves(self):
        self.assertIsNone(triage.skip_reason("how does that work?", ["prior"]))

    def test_a_question_mark_does_not_rescue_a_bare_imperative(self):
        """Pre-existing behaviour, asserted so it is a decision rather than an
        accident: the "?" escape guards the directive rule, and the bare
        imperative is checked first. "update that again?" still names no object
        of its own, so treating it as a continuation is right -- but the reason
        is the missing object, not the punctuation."""
        self.assertIsNotNone(triage.skip_reason("update that again?", ["prior"]))

    def test_a_real_request_retrieves(self):
        self.assertIsNone(triage.skip_reason("how does the stale claim sweep work", []))

    def test_a_request_naming_its_own_object_retrieves(self):
        self.assertIsNone(triage.skip_reason("add a health probe to compile", ["prior"]))


class SelectionTests(unittest.TestCase):
    """A skip invalidates similarity, not recency."""

    def test_skipping_keeps_ephemeral(self):
        with patch.object(memory_select, "_select_ephemeral", return_value=[{"id": "e1"}]), \
             patch.object(memory_select, "_select_persistent",
                          side_effect=AssertionError("searched anyway")):
            eph, per = memory_select.select_for_frame(
                "yes", context=_ctx(), similarity_search=False)
        self.assertEqual([r["id"] for r in eph], ["e1"])
        self.assertEqual(per, [])

    def test_not_skipping_searches(self):
        with patch.object(memory_select, "_select_ephemeral", return_value=[]), \
             patch.object(memory_select, "_select_persistent", return_value=[{"id": "p1"}]):
            _eph, per = memory_select.select_for_frame("a real question", context=_ctx())
        self.assertEqual([r["id"] for r in per], ["p1"])


class EmbedCostTests(unittest.TestCase):
    def test_a_skipped_turn_costs_no_embedding_call(self):
        with patch.object(packet, "recent_assistant_turns", return_value=["prior"]), \
             patch.object(packet, "embed_text_sync",
                          side_effect=AssertionError("embedded a non-query")), \
             patch.object(packet.memory_select, "select_for_frame", return_value=([], [])):
            packet._load_memories("yes")


def _ctx():
    from arteries.memory_select import AgentContext
    from arteries.cli_caps import get_capabilities

    return AgentContext(cli="generic", project_id="p", agent_id="a",
                        parent_agent_id=None, agent_role="parent",
                        event="prompt", capabilities=get_capabilities("generic"))
