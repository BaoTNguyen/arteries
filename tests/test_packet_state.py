"""The compaction renderer (planning/compaction_v3.md §2): state fields
instead of provenance tiers, picked by ARTERIES_EVENT=compact rather than the
message content, so retrieval calls are provably unaffected.
"""

import json
import uuid
from unittest.mock import patch

import pytest

from arteries import packet, storage


class TestTriggerDispatch:
    def test_no_event_env_uses_the_tier_renderer(self):
        with patch.object(packet.memory_select, "select_for_frame", return_value=([], [])):
            text = packet.build_packet("hello")
        assert "## Current Context" in text
        assert "## Objective" not in text

    def test_compact_event_env_uses_the_state_renderer(self, monkeypatch):
        monkeypatch.setenv("ARTERIES_EVENT", "compact")
        with patch.object(packet, "render_state", return_value="STATE") as mocked:
            assert packet.build_packet("auto compact") == "STATE"
        assert mocked.called


class TestRenderState:
    @pytest.fixture(autouse=True)
    def _setup(self, test_db, monkeypatch):
        self.project_id = f"test-{uuid.uuid4().hex[:8]}"
        self.session_id = f"sess-{uuid.uuid4().hex[:8]}"
        monkeypatch.setenv("ARTERIES_EVENT", "compact")
        monkeypatch.setenv("ARTERIES_SESSION_ID", self.session_id)
        monkeypatch.setattr(packet, "PROJECT_ID", self.project_id)
        self.run_id = self._make_run()

    def _make_run(self) -> str:
        with storage._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO arteries.agent_runs (project_id, cli, agent_id, metadata) "
                "VALUES (%s, 'claude', 'test-agent', %s::jsonb) RETURNING id",
                (self.project_id, json.dumps({"session_id": self.session_id})),
            )
            run_id = str(cur.fetchone()[0])
            conn.commit()
        return run_id

    def _tool_event(self, tool: str, target: str, failed: bool = False, exit_code: int = 0):
        with storage._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO arteries.agent_events (run_id, project_id, source, event_type, payload) "
                "VALUES (%s, %s, 'arteries', 'tool.result', %s::jsonb)",
                (self.run_id, self.project_id,
                 json.dumps({"tool": tool, "target": target, "failed": failed,
                            "exit_code": exit_code})),
            )
            conn.commit()

    def test_state_sections_render_in_place_of_tiers(self):
        text = packet.render_state("auto compact", {})
        for title in packet.STATE_SECTION_TITLES:
            assert f"## {title}" in text
        assert "## Ephemeral Memory" not in text
        assert "## Persistent Memory" not in text

    def test_canary_is_present_and_unique_per_build(self):
        first = packet.render_state("auto compact", {})
        second = packet.render_state("auto compact", {})
        canary_first = [l for l in first.splitlines() if l.startswith("Canary: ")][0]
        canary_second = [l for l in second.splitlines() if l.startswith("Canary: ")][0]
        assert canary_first != canary_second

    def test_constraints_come_from_persistent_preferences_and_constraints(self):
        storage.insert_persistent(self.project_id, "Prefer stdlib over new deps.",
                                  ["technical"], kind="preference")
        storage.insert_persistent(self.project_id, "Never sudo the agent.",
                                  ["technical"], kind="constraint")
        storage.insert_persistent(self.project_id, "The sky is blue.",
                                  ["technical"], kind="fact")
        text = packet.render_state("auto compact", {})
        assert "Prefer stdlib over new deps." in text
        assert "Never sudo the agent." in text
        assert "The sky is blue." not in text

    def test_settled_decisions_come_from_persistent_kind_decision(self):
        storage.insert_persistent(self.project_id, "Chose watermark chaining over string dedupe.",
                                  ["technical"], kind="decision")
        text = packet.render_state("auto compact", {})
        assert "Chose watermark chaining over string dedupe." in text

    def test_mid_session_decision_marker_surfaces_from_ephemeral(self):
        storage.insert_ephemeral(self.project_id, "test-agent",
                                 "Let's use halfvec instead of the old vector type.",
                                 ["technical"], session_id=self.session_id, source="user")
        text = packet.render_state("auto compact", {})
        assert "halfvec" in text

    def test_successful_mutation_renders_under_done(self):
        self._tool_event("Write", "/tmp/foo.py")
        text = packet.render_state("auto compact", {})
        done_section = text.split("## Done")[1].split("## In Progress")[0]
        assert "/tmp/foo.py" in done_section

    def test_failed_call_renders_under_blocked_never_done(self):
        self._tool_event("Bash", "pytest -x", failed=True, exit_code=1)
        text = packet.render_state("auto compact", {})
        done_section = text.split("## Done")[1].split("## In Progress")[0]
        blocked_section = text.split("## Blocked")[1].split("## Retracted")[0]
        assert "pytest -x" not in done_section
        assert "pytest -x" in blocked_section

    def test_retracted_is_none_with_nothing_to_detect(self):
        text = packet.render_state("auto compact", {})
        assert "## Retracted\n\n(none)" in text

    def test_unresolved_always_renders_none_before_commit_4_lands(self):
        text = packet.render_state("auto compact", {})
        assert "## Unresolved\n\n(none)" in text

    def _supersede_edge(self, reason: str = "cosine 0.91, differing literal"):
        old_id = storage.insert_persistent(self.project_id, "EMBED_DIM is 1024.", ["technical"])
        new_id = storage.insert_persistent(self.project_id, "config.py reads EMBED_DIM = 768.",
                                           ["technical"])
        with storage._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE arteries.persistent SET valid_until = now() WHERE id = %s", (old_id,))
            cur.execute(
                "INSERT INTO arteries.memory_edges "
                "(project_id, src_kind, src_id, dst_kind, dst_id, rel, metadata) "
                "VALUES (%s, 'persistent', %s, 'persistent', %s, 'supersedes', %s::jsonb)",
                (self.project_id, new_id, old_id, json.dumps({"reason": reason})),
            )
            conn.commit()
        return old_id, new_id

    def test_supersede_edge_is_a_dry_run_candidate_not_rendered(self, monkeypatch):
        with patch.object(packet.runlog, "log_event") as logged:
            self._supersede_edge()
            text = packet.render_state("auto compact", {})
        assert "## Retracted\n\n(none)" in text
        assert logged.called
        assert logged.call_args[0][0] == "memory.retraction.candidate"
        assert logged.call_args[0][2]["dry_run"] is True

    def test_supersede_edge_renders_once_dry_run_is_off(self, monkeypatch):
        monkeypatch.setenv("ARTERIES_RETRACTION_DRY_RUN", "off")
        monkeypatch.setattr(packet, "RETRACTION_DRY_RUN", False)
        self._supersede_edge()
        text = packet.render_state("auto compact", {})
        section = text.split("## Retracted")[1].split("## Unresolved")[0]
        assert "EMBED_DIM is 1024" in section
        assert "config.py reads EMBED_DIM = 768" in section

    def test_supersede_edge_older_than_covers_from_is_not_resurfaced(self, monkeypatch):
        """§4.5: a retraction renders once. An edge from before the window this
        packet covers is one the previous packet already showed."""
        monkeypatch.setattr(packet, "RETRACTION_DRY_RUN", False)
        old_id, new_id = self._supersede_edge()
        with storage._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE arteries.memory_edges SET created_at = now() - interval '10 days' "
                "WHERE project_id = %s", (self.project_id,),
            )
            conn.commit()
        text = packet.render_state("auto compact", {})
        assert "## Retracted\n\n(none)" in text

    def test_tool_refutation_detector_flags_a_success_claim_beside_a_failure(self, monkeypatch):
        monkeypatch.setattr(packet, "RETRACTION_DRY_RUN", False)
        storage.insert_ephemeral(self.project_id, "test-agent", "The tests pass now.",
                                 ["technical"], session_id=self.session_id, source="assistant")
        self._tool_event("Bash", "pytest -x", failed=True, exit_code=1)
        text = packet.render_state("auto compact", {})
        section = text.split("## Retracted")[1].split("## Unresolved")[0]
        assert "The tests pass now." in section
        assert "pytest -x" in section

    def _embedded_pair(self, fact_a: str, fact_b: str, source_a: str = "assistant",
                       source_b: str = "assistant"):
        # Same embedding for both -- cosine 1.0 clears the 0.85 detector-3
        # floor without needing a real embed call in a test.
        vec = [0.1] * 1024
        id_a = storage.insert_ephemeral(self.project_id, "test-agent", fact_a, ["technical"],
                                        session_id=self.session_id, source=source_a,
                                        embedding=vec)
        id_b = storage.insert_ephemeral(self.project_id, "test-agent", fact_b, ["technical"],
                                        session_id=self.session_id, source=source_b,
                                        embedding=vec)
        return id_a, id_b

    def test_value_overwrite_is_detected_between_near_duplicate_claims(self, monkeypatch):
        monkeypatch.setattr(packet, "RETRACTION_DRY_RUN", False)
        self._embedded_pair("The packet budget defaults to 6000.",
                            "The packet budget defaults to 20000.")
        text = packet.render_state("auto compact", {})
        section = text.split("## Retracted")[1].split("## Unresolved")[0]
        assert "6000" in section and "20000" in section

    def test_value_overwrite_respects_evidence_precedence(self, monkeypatch):
        """A user statement beats an assistant one regardless of which came
        first (§4.3: stronger wins across classes, not recency)."""
        monkeypatch.setattr(packet, "RETRACTION_DRY_RUN", False)
        with storage._conn() as conn, conn.cursor() as cur:
            self._embedded_pair("The packet budget defaults to 6000.",
                                "The packet budget defaults to 20000.",
                                source_a="user", source_b="assistant")
            # Make the (weaker) assistant claim the later one, so a
            # recency-only rule would pick it -- precedence must not.
            cur.execute(
                "UPDATE arteries.ephemeral SET source_ts = now() + interval '1 hour' "
                "WHERE project_id=%s AND source='assistant'", (self.project_id,))
            conn.commit()
        text = packet.render_state("auto compact", {})
        section = text.split("## Retracted")[1].split("## Unresolved")[0]
        assert "Believed: The packet budget defaults to 20000." in section
        assert "Refuted by: The packet budget defaults to 6000." in section

    def test_value_overwrite_with_no_ordering_goes_to_unresolved(self, monkeypatch):
        """Same evidence class, identical timestamps -- a genuine tie never
        gets silently picked (§4.3)."""
        monkeypatch.setattr(packet, "RETRACTION_DRY_RUN", False)
        with storage._conn() as conn, conn.cursor() as cur:
            self._embedded_pair("The packet budget defaults to 6000.",
                                "The packet budget defaults to 20000.")
            cur.execute(
                "UPDATE arteries.ephemeral SET source_ts = '2026-01-01T00:00:00Z' "
                "WHERE project_id=%s", (self.project_id,))
            conn.commit()
        text = packet.render_state("auto compact", {})
        retracted_section = text.split("## Retracted")[1].split("## Unresolved")[0]
        unresolved_section = text.split("## Unresolved")[1].split("## Open Question")[0]
        assert retracted_section.strip() == "(none)"
        assert "6000" in unresolved_section and "20000" in unresolved_section

    def test_near_miss_guard_drops_different_subjects(self, monkeypatch):
        """The v2 worked example, kept as the regression case: a script
        deliberately leaves RERANKER_DEVICE unset, .arteries/env sets it to
        cuda:1 -- cosine high, differing literal, but different files, both
        true (planning/compaction_v3.md §4.4)."""
        monkeypatch.setattr(packet, "RETRACTION_DRY_RUN", False)
        self._embedded_pair("compact-packet.sh leaves RERANKER_DEVICE unset.",
                            ".arteries/env sets RERANKER_DEVICE to cuda:1.")
        text = packet.render_state("auto compact", {})
        assert "## Retracted\n\n(none)" in text
        assert "## Unresolved\n\n(none)" in text

    def test_user_correction_detector_flags_a_negation_after_an_assistant_claim(self, monkeypatch):
        monkeypatch.setattr(packet, "RETRACTION_DRY_RUN", False)
        with storage._conn() as conn, conn.cursor() as cur:
            storage.insert_ephemeral(self.project_id, "test-agent",
                                     "The packet budget defaults to 6000.",
                                     ["technical"], session_id=self.session_id,
                                     source="assistant")
            cur.execute(
                "UPDATE arteries.ephemeral SET source_ts = now() - interval '1 minute' "
                "WHERE project_id=%s", (self.project_id,))
            conn.commit()
        storage.insert_ephemeral(self.project_id, "test-agent",
                                 "no, it's 20000, I changed it last week.",
                                 ["technical"], session_id=self.session_id, source="user")
        text = packet.render_state("auto compact", {})
        section = text.split("## Retracted")[1].split("## Unresolved")[0]
        assert "The packet budget defaults to 6000." in section
        assert "20000" in section

    def test_open_question_is_the_trailing_unanswered_assistant_turn(self):
        event = {"messages": [
            {"role": "user", "content": "How should compaction detect retraction?"},
            {"role": "assistant", "content": "Should it run before or after promotion?"},
        ]}
        text = packet.render_state("auto compact", event)
        section = text.split("## Open Question")[1].split("## Files")[0]
        assert "Should it run before or after promotion?" in section

    def test_files_are_deduped_and_capped_at_five_most_recent(self):
        for i in range(7):
            self._tool_event("Write", f"/tmp/file{i}.py")
        self._tool_event("Write", "/tmp/file6.py")  # repeat of the most recent
        text = packet.render_state("auto compact", {})
        section = text.split("## Files")[1].split("## Next")[0]
        paths = [l for l in section.splitlines() if l.strip()]
        assert len(paths) == 5
        assert "/tmp/file6.py" in paths  # most recent survives
        assert "/tmp/file0.py" not in paths  # oldest is dropped first

    def test_dropped_is_none_when_nothing_overflows(self):
        text = packet.render_state("auto compact", {})
        assert "## Dropped\n\n(none)" in text

    def test_protected_sections_survive_a_tiny_budget_intact(self):
        """The never-drop fields (planning/compaction_v3.md §5) must not be
        eaten by the droppable sections' overflow, however small the budget."""
        for i in range(30):
            storage.insert_persistent(self.project_id, f"Constraint number {i} about something.",
                                      ["technical"], kind="constraint")
        self._tool_event("Bash", "pytest -x", failed=True, exit_code=1)
        text = packet.render_state("auto compact", {}, budget=2000)
        assert "pytest -x" in text.split("## Blocked")[1].split("## Retracted")[0]
        assert "## Use Rules" in text
        assert "[Packet truncated to fit budget.]" not in text

    def test_constraints_are_named_in_dropped_when_truncated(self):
        for i in range(50):
            storage.insert_persistent(self.project_id,
                                      f"Constraint number {i} about something fairly long here.",
                                      ["technical"], kind="constraint")
        text = packet.render_state("auto compact", {}, budget=1500)
        dropped_section = text.split("## Dropped")[1].split("## Use Rules")[0]
        assert "Constraints" in dropped_section

    def test_record_packet_is_called_even_with_no_ranked_members(self):
        packet.render_state("auto compact", {})
        with storage._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT covers_from, covers_to, body FROM arteries.packets "
                "WHERE project_id=%s AND session_id=%s", (self.project_id, self.session_id),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] is not None and row[1] is not None
        assert "## Objective" in row[2]

    def test_second_packet_chains_off_the_first_via_covers_to(self):
        packet.render_state("auto compact", {})
        with storage._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, covers_to FROM arteries.packets "
                "WHERE project_id=%s AND session_id=%s", (self.project_id, self.session_id),
            )
            first_id, first_covers_to = cur.fetchone()

        packet.render_state("auto compact", {})
        with storage._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT previous_id, covers_from FROM arteries.packets "
                "WHERE project_id=%s AND session_id=%s ORDER BY created_at DESC LIMIT 1",
                (self.project_id, self.session_id),
            )
            previous_id, covers_from = cur.fetchone()
        assert str(previous_id) == str(first_id)
        assert covers_from == first_covers_to
