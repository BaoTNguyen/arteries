"""A sandboxed agent turn gets no Recent Conversation; interactive is unchanged."""
import pytest

from arteries import packet

STUB = "ZEBRA-STUB-QUESTION"
VARS = ("ARTERIES_AGENT_ROLE", "ARTERIES_LANE", "ARTERIES_TRUST", "ARTERIES_EVENT")


@pytest.fixture
def stubbed(monkeypatch, tmp_path):
    monkeypatch.setenv("VASCULAR_HOME", str(tmp_path))
    for v in VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(packet, "_load_recent_pairs", lambda event, limit=10: [packet.RecentPair(STUB, "answer")])
    monkeypatch.setattr(packet, "_load_memories", lambda *a, **k: [])
    monkeypatch.setattr(packet, "_corpus_suggestion", lambda *a, **k: {})
    monkeypatch.setattr(packet, "_query_embedding", lambda m: None)
    monkeypatch.setattr(packet, "_current_context", lambda m, e: ["Project: x"])
    return monkeypatch


@pytest.mark.parametrize("var,value", [
    ("ARTERIES_AGENT_ROLE", "implement"), ("ARTERIES_LANE", "web"), ("ARTERIES_TRUST", "untrusted")])
def test_sandboxed_omits_recent_conversation(stubbed, var, value):
    stubbed.setenv(var, value)
    out = packet.build_packet("hi")
    assert "Recent Conversation" not in out and STUB not in out
    assert "Project: x" in out


def test_interactive_keeps_recent_conversation(stubbed):
    out = packet.build_packet("hi")
    assert "Recent Conversation" in out and STUB in out


def test_sandboxed_with_nothing_else_returns_empty(stubbed):
    stubbed.setenv("ARTERIES_AGENT_ROLE", "implement")
    stubbed.setattr(packet, "_current_context", lambda m, e: [])
    assert packet.build_packet("") == ""
    stubbed.delenv("ARTERIES_AGENT_ROLE")
    assert packet.build_packet("") != ""


def test_compaction_path_skips_pairs(stubbed):
    stubbed.setenv("ARTERIES_AGENT_ROLE", "implement")
    stubbed.setattr(packet.storage, "_env_session_id", lambda: None)
    stubbed.setattr(packet, "_session_window", lambda sid: (None, None))
    stubbed.setattr(packet.storage, "tool_results_since", lambda *a, **k: [])
    stubbed.setattr(packet.storage, "get_ephemeral", lambda *a, **k: [])
    stubbed.setattr(packet.storage, "record_packet", lambda *a, **k: None)
    stubbed.setattr(packet, "_detect_value_overwrites", lambda *a, **k: ([], []))
    stubbed.setattr(packet, "_detect_supersede_edges", lambda *a, **k: [])
    stubbed.setattr(packet, "_mid_session_decisions", lambda e: [])
    stubbed.setattr(packet, "_constraints", lambda: [])
    out = packet.render_state("hi", {})
    assert STUB not in out
    stubbed.delenv("ARTERIES_AGENT_ROLE")
    assert STUB in packet.render_state("hi", {})
